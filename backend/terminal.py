import asyncio
import fcntl
import json
import logging
import os
import pty
import struct
import termios
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import get_session
from .config import ADB_PATH
from .devices import get_devices
from .reservations import get_active_reservation

logger = logging.getLogger(__name__)
router = APIRouter()

SPAWN_TIMEOUT = 10          # seconds to wait for adb shell to start
PTY_WATCHDOG_INTERVAL = 30  # seconds of silence before checking process liveness
WS_PING_INTERVAL = 15       # seconds between WebSocket pings


# ── Helpers ────────────────────────────────────────────────────────────────────

def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Resize the PTY window (sends SIGWINCH to the child)."""
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError:
        pass


def _child_setup() -> None:
    """Run in the child process before exec: create new session and set
    the slave PTY as the controlling terminal so job control / Ctrl-C work."""
    os.setsid()
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)  # 0 = stdin = slave PTY
    except Exception:
        pass


def _safe_close_fd(fd: int) -> None:
    """Close a file descriptor, ignoring errors from double-close."""
    try:
        os.close(fd)
    except OSError:
        pass


# ── WebSocket endpoint ─────────────────────────────────────────────────────────

@router.websocket("/ws/terminal/{serial}")
async def ws_terminal(websocket: WebSocket, serial: str):
    # ── Auth ──────────────────────────────────────────────────────────────────
    session = get_session(websocket)
    if not session:
        await websocket.close(code=1008)   # Policy Violation — not authed
        return

    display_name = session["display_name"]

    # ── Device must be online ─────────────────────────────────────────────────
    if serial not in get_devices():
        await websocket.close(code=4004)   # device not found / offline
        return

    # ── Device must be reserved by THIS user ──────────────────────────────────
    reservation = await get_active_reservation(serial)
    if not reservation or reservation["reserved_by"] != display_name:
        await websocket.close(code=4003)   # not reserved by you
        return

    await websocket.accept()
    logger.info("Terminal open: serial=%s user=%s", serial, display_name)

    # ── Spawn adb shell with a real PTY ───────────────────────────────────────
    master_fd, slave_fd = pty.openpty()
    _set_winsize(master_fd, 24, 80)
    master_closed = False

    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                ADB_PATH, "-s", serial, "shell",
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                preexec_fn=_child_setup,
            ),
            timeout=SPAWN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error("adb shell spawn timed out for %s (device unresponsive)", serial)
        _safe_close_fd(master_fd)
        _safe_close_fd(slave_fd)
        try:
            await websocket.send_text(
                json.dumps({"type": "error", "message": "Device unresponsive — adb shell timed out"})
            )
        except Exception:
            pass
        await websocket.close(code=4000)
        return
    except Exception as exc:
        logger.error("adb shell spawn failed for %s: %s", serial, exc)
        _safe_close_fd(master_fd)
        _safe_close_fd(slave_fd)
        try:
            await websocket.send_text(
                json.dumps({"type": "error", "message": f"Failed to start shell: {exc}"})
            )
        except Exception:
            pass
        await websocket.close(code=4000)
        return

    _safe_close_fd(slave_fd)   # Parent doesn't need the slave end

    loop = asyncio.get_running_loop()
    output_q: asyncio.Queue = asyncio.Queue()
    last_pty_data = time.monotonic()

    # ── PTY → queue (called from event loop when fd is readable) ─────────────
    reader_active = True

    def _on_readable() -> None:
        nonlocal last_pty_data, reader_active
        try:
            data = os.read(master_fd, 4096)
            if data:
                last_pty_data = time.monotonic()
                output_q.put_nowait(data)
            else:
                # EOF — child exited
                output_q.put_nowait(None)
                if reader_active:
                    reader_active = False
                    try:
                        loop.remove_reader(master_fd)
                    except Exception:
                        pass
        except OSError:
            # PTY closed (child exited)
            output_q.put_nowait(None)
            if reader_active:
                reader_active = False
                try:
                    loop.remove_reader(master_fd)
                except Exception:
                    pass

    loop.add_reader(master_fd, _on_readable)

    # ── Task: drain queue → WebSocket ─────────────────────────────────────────
    async def pty_to_ws() -> None:
        try:
            while True:
                data = await output_q.get()
                if data is None:
                    # Process ended — notify frontend
                    try:
                        await websocket.send_text(json.dumps({
                            "type": "exit",
                            "message": "Session ended — device may have disconnected",
                        }))
                    except Exception:
                        pass
                    break
                try:
                    await websocket.send_bytes(data)
                except Exception as exc:
                    logger.debug("pty_to_ws send failed: %s", exc)
                    break
        except Exception as exc:
            logger.debug("pty_to_ws ended: %s", exc)

    # ── Task: WebSocket → PTY ─────────────────────────────────────────────────
    async def ws_to_pty() -> None:
        try:
            while True:
                msg = await websocket.receive()

                if msg["type"] == "websocket.disconnect":
                    break

                raw = msg.get("bytes")
                if raw:
                    # Raw input bytes (keystrokes, paste, etc.)
                    try:
                        os.write(master_fd, raw)
                    except OSError:
                        break

                text = msg.get("text")
                if text:
                    # Control message: {"type":"resize","cols":N,"rows":N}
                    try:
                        cmd = json.loads(text)
                        if cmd.get("type") == "resize":
                            rows = max(1, int(cmd.get("rows", 24)))
                            cols = max(1, int(cmd.get("cols", 80)))
                            _set_winsize(master_fd, rows, cols)
                        elif cmd.get("type") == "pong":
                            pass  # heartbeat response from client
                    except (json.JSONDecodeError, ValueError, OSError):
                        pass

        except (WebSocketDisconnect, Exception) as exc:
            logger.debug("ws_to_pty ended: %s", exc)

    # ── Task: watchdog — check process liveness + WebSocket heartbeat ─────────
    async def watchdog() -> None:
        try:
            while True:
                await asyncio.sleep(WS_PING_INTERVAL)

                # Send ping to detect dead WebSocket connections
                try:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                except Exception:
                    logger.info("Watchdog: WebSocket send failed, connection dead")
                    break

                # Check if process is still alive
                if proc.returncode is not None:
                    logger.info("Watchdog: adb process exited (rc=%s) for %s",
                                proc.returncode, serial)
                    # Push sentinel so pty_to_ws exits
                    output_q.put_nowait(None)
                    break

                # Check for PTY silence
                silence = time.monotonic() - last_pty_data
                if silence > PTY_WATCHDOG_INTERVAL:
                    # Process might be hung — check if alive
                    if proc.returncode is not None:
                        logger.info("Watchdog: process dead after %ds silence for %s",
                                    int(silence), serial)
                        output_q.put_nowait(None)
                        break
                    else:
                        logger.debug("Watchdog: %ds silence but process alive for %s",
                                     int(silence), serial)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Watchdog ended: %s", exc)

    # ── Run all three tasks; stop when any finishes ───────────────────────────
    pty_task = asyncio.create_task(pty_to_ws())
    ws_task  = asyncio.create_task(ws_to_pty())
    wd_task  = asyncio.create_task(watchdog())

    try:
        done, pending = await asyncio.wait(
            [pty_task, ws_task, wd_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        # Remove fd watcher before closing
        if reader_active:
            reader_active = False
            try:
                loop.remove_reader(master_fd)
            except Exception:
                pass

        # Kill subprocess
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (asyncio.TimeoutError, Exception):
            pass

        # Close master PTY (only once)
        if not master_closed:
            master_closed = True
            _safe_close_fd(master_fd)

        # Close WebSocket
        try:
            await websocket.close()
        except Exception:
            pass

        logger.info("Terminal closed: serial=%s user=%s", serial, display_name)
