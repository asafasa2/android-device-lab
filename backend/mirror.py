import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import get_session
from .config import ADB_PATH
from .devices import get_devices
from .reservations import get_active_reservation

logger = logging.getLogger(__name__)
router = APIRouter()

PNG_MAGIC = b"\x89PNG"
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"

SCREENCAP_TIMEOUT = 5.0
SCREENCAP_INTERVAL = 1.0       # 1 FPS fallback
CONSECUTIVE_FAIL_LIMIT = 3
SCREENRECORD_MAX_SEC = 170     # restart before 180s hard limit

HAS_FFMPEG = shutil.which("ffmpeg") is not None
if HAS_FFMPEG:
    logger.info("ffmpeg found — screenrecord streaming enabled")
else:
    logger.info("ffmpeg not found — using screencap fallback (1 FPS)")


# ── Screen info ───────────────────────────────────────────────────────────────

def _get_screen_size(serial: str) -> tuple[int, int]:
    """Return (width, height) from `adb shell wm size`. Falls back to 1080x1920."""
    try:
        result = subprocess.run(
            [ADB_PATH, "-s", serial, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        )
        logger.info("Mirror wm size for %s: stdout=%r rc=%d",
                     serial, result.stdout.strip(), result.returncode)
        m = re.search(r"(\d+)x(\d+)", result.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception as e:
        logger.warning("wm size failed for %s: %s", serial, e)
    return 1080, 1920


def _get_orientation(serial: str) -> int:
    """Return current display orientation (0=portrait, 1=landscape CW, 2=reverse, 3=landscape CCW)."""
    try:
        result = subprocess.run(
            [ADB_PATH, "-s", serial, "shell", "dumpsys", "input"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"SurfaceOrientation:\s*(\d)", result.stdout)
        if m:
            return int(m.group(1))
    except Exception as e:
        logger.debug("orientation check failed for %s: %s", serial, e)
    return 0


async def _check_screen_on(serial: str) -> bool:
    """Check if the device screen is on."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH, "-s", serial, "shell", "dumpsys", "power",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        output = stdout.decode(errors="replace")
        if "Display Power: state=OFF" in output:
            return False
    except Exception as e:
        logger.debug("screen-on check failed for %s: %s", serial, e)
    return True


# ── Screencap (fallback) ──────────────────────────────────────────────────────

async def _screencap_execout(serial: str) -> bytes | None:
    """Run `adb exec-out screencap -p` and return PNG bytes."""
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH, "-s", serial, "exec-out", "screencap", "-p",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SCREENCAP_TIMEOUT)
        elapsed = time.monotonic() - t0
        if proc.returncode != 0 or not stdout or len(stdout) < 8:
            logger.warning("screencap failed for %s: rc=%d size=%d (%.1fs)",
                           serial, proc.returncode, len(stdout) if stdout else 0, elapsed)
            return None
        if not stdout[:4].startswith(PNG_MAGIC[:4]):
            logger.warning("screencap non-PNG for %s: first 8=%r (%.1fs)",
                           serial, stdout[:8], elapsed)
            return None
        logger.debug("screencap OK for %s: %d bytes (%.1fs)", serial, len(stdout), elapsed)
        return stdout
    except asyncio.TimeoutError:
        logger.warning("screencap timed out for %s", serial)
        return None
    except Exception as e:
        logger.warning("screencap error for %s: %s", serial, e)
        return None


# ── Persistent ADB shell for fast input ──────────────────────────────────────

class AdbInputShell:
    """Keeps a persistent `adb shell` process open and pipes input commands
    through stdin.  ~10x faster than spawning a new process per tap."""

    def __init__(self, serial: str):
        self._serial = serial
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> bool:
        """Open the persistent shell. Returns True on success."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                ADB_PATH, "-s", self._serial, "shell",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            logger.info("Persistent input shell opened for %s (pid=%d)",
                        self._serial, self._proc.pid)
            return True
        except Exception as e:
            logger.warning("Failed to open persistent input shell for %s: %s",
                           self._serial, e)
            self._proc = None
            return False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _send(self, cmd: str) -> bool:
        """Write a command line to the shell's stdin."""
        if not self.alive:
            logger.warning("Input shell dead for %s — respawning", self._serial)
            if not await self.start():
                return False

        try:
            self._proc.stdin.write((cmd + "\n").encode())
            await self._proc.stdin.drain()
            logger.debug("input shell cmd for %s: %s", self._serial, cmd)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            logger.warning("Input shell write failed for %s: %s — respawning",
                           self._serial, e)
            await self.close()
            if await self.start():
                try:
                    self._proc.stdin.write((cmd + "\n").encode())
                    await self._proc.stdin.drain()
                    return True
                except Exception as e2:
                    logger.warning("Input shell retry failed for %s: %s",
                                   self._serial, e2)
            return False
        except Exception as e:
            logger.warning("Input shell error for %s: %s", self._serial, e)
            return False

    async def tap(self, x: int, y: int) -> bool:
        cmd = f"input tap {x} {y}"
        logger.info("Mirror tap for %s: px=(%d, %d)", self._serial, x, y)
        return await self._send(cmd)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: int) -> bool:
        cmd = f"input swipe {x1} {y1} {x2} {y2} {duration}"
        logger.info("Mirror swipe for %s: (%d,%d)->(%d,%d) %dms",
                     self._serial, x1, y1, x2, y2, duration)
        return await self._send(cmd)

    async def keyevent(self, keycode: int) -> bool:
        cmd = f"input keyevent {keycode}"
        logger.info("Mirror keyevent for %s: %d", self._serial, keycode)
        return await self._send(cmd)

    async def text(self, text: str) -> bool:
        safe = text.replace("\\", "\\\\").replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        cmd = f"input text {safe}"
        logger.info("Mirror text for %s: %r", self._serial, text[:50])
        return await self._send(cmd)

    async def close(self):
        """Terminate the persistent shell."""
        if self._proc is None:
            return
        pid = self._proc.pid
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        self._proc = None
        logger.info("Persistent input shell closed for %s (pid=%d)", self._serial, pid)


# ── Screenrecord + ffmpeg streaming ──────────────────────────────────────────

async def _stream_screenrecord(serial: str, stop_event: asyncio.Event,
                                websocket: WebSocket) -> None:
    """Stream JPEG frames via screenrecord piped through ffmpeg.
    Restarts the pipeline when screenrecord hits its time limit."""

    safe_adb = shlex.quote(ADB_PATH)
    safe_serial = shlex.quote(serial)

    while not stop_event.is_set():
        logger.info("Starting screenrecord+ffmpeg pipeline for %s", serial)

        cmd = (
            f"{safe_adb} -s {safe_serial} exec-out screenrecord"
            f" --output-format=h264 --time-limit {SCREENRECORD_MAX_SEC} - | "
            f"ffmpeg -loglevel error -probesize 500k -analyzeduration 500k"
            f" -i pipe:0 -f image2pipe"
            f" -vf 'fps=5,scale=-1:720'"
            f" -q:v 5 -c:v mjpeg pipe:1"
        )
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        buf = b""
        frames_sent = 0
        t0 = time.monotonic()

        try:
            while not stop_event.is_set():
                try:
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.warning("screenrecord read timeout for %s", serial)
                    break

                if not chunk:
                    break  # process ended

                buf += chunk

                # Extract complete JPEG frames from the stream
                while True:
                    soi = buf.find(JPEG_SOI)
                    if soi == -1:
                        buf = b""
                        break
                    eoi = buf.find(JPEG_EOI, soi + 2)
                    if eoi == -1:
                        # Trim garbage before SOI
                        buf = buf[soi:]
                        break

                    frame = buf[soi:eoi + 2]
                    buf = buf[eoi + 2:]

                    try:
                        await websocket.send_bytes(frame)
                        frames_sent += 1
                    except Exception:
                        stop_event.set()
                        return

        except Exception as e:
            logger.warning("screenrecord stream error for %s: %s", serial, e)
        finally:
            elapsed = time.monotonic() - t0
            logger.info("screenrecord session ended for %s: %d frames in %.1fs",
                        serial, frames_sent, elapsed)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                pass

        if not stop_event.is_set():
            logger.info("Restarting screenrecord pipeline for %s", serial)
            await asyncio.sleep(0.3)


# ── WebSocket endpoint ───────────────────────────────────────────────────────

@router.websocket("/ws/mirror/{serial}")
async def ws_mirror(websocket: WebSocket, serial: str):
    # ── Auth ──────────────────────────────────────────────────────────────────
    session = get_session(websocket)
    if not session:
        await websocket.close(code=1008)
        return

    display_name = session["display_name"]

    # ── Device must be online ─────────────────────────────────────────────────
    if serial not in get_devices():
        await websocket.close(code=4004)
        return

    # ── Device must be reserved by THIS user ──────────────────────────────────
    reservation = await get_active_reservation(serial)
    if not reservation or reservation["reserved_by"] != display_name:
        await websocket.close(code=4003)
        return

    await websocket.accept()
    logger.info("Mirror open: serial=%s user=%s", serial, display_name)

    # Get screen dimensions and orientation
    phys_w, phys_h = _get_screen_size(serial)
    orientation = _get_orientation(serial)

    if orientation in (1, 3):
        width, height = phys_h, phys_w
    else:
        width, height = phys_w, phys_h

    use_streaming = HAS_FFMPEG
    logger.info("Mirror for %s: physical=%dx%d orientation=%d display=%dx%d mode=%s",
                serial, phys_w, phys_h, orientation, width, height,
                "streaming" if use_streaming else "screencap")

    await websocket.send_text(json.dumps({
        "type": "info",
        "width": width,
        "height": height,
        "serial": serial,
        "mode": "streaming" if use_streaming else "screencap",
    }))

    stop_event = asyncio.Event()

    # ── Persistent input shell ─────────────────────────────────────────────────
    input_shell = AdbInputShell(serial)
    await input_shell.start()

    # ── Frame loop ────────────────────────────────────────────────────────────
    async def frame_loop() -> None:
        if use_streaming:
            await _stream_screenrecord(serial, stop_event, websocket)
        else:
            await _screencap_loop(serial, stop_event, websocket)

    async def _screencap_loop(ser, stop, ws):
        consecutive_fails = 0
        while not stop.is_set():
            t = asyncio.get_event_loop().time()
            png = await _screencap_execout(ser)
            if stop.is_set():
                break
            try:
                if png:
                    consecutive_fails = 0
                    await ws.send_bytes(png)
                else:
                    consecutive_fails += 1
                    if consecutive_fails >= 2:
                        screen_on = await _check_screen_on(ser)
                        if not screen_on:
                            await ws.send_text(json.dumps({
                                "type": "screen_off",
                                "message": "Device screen is off — tap to wake",
                            }))
                    if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "screencap failed — device may not support screen capture",
                        }))
            except Exception:
                break
            elapsed = asyncio.get_event_loop().time() - t
            sleep_for = max(0.0, SCREENCAP_INTERVAL - elapsed)
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_for)
                break
            except asyncio.TimeoutError:
                pass

    # ── Input loop ────────────────────────────────────────────────────────────
    async def input_loop() -> None:
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                text = msg.get("text")
                if not text:
                    continue
                try:
                    ev = json.loads(text)
                except json.JSONDecodeError:
                    continue

                ev_type = ev.get("type")

                if ev_type == "tap":
                    nx = float(ev.get("x", 0))
                    ny = float(ev.get("y", 0))
                    px = int(nx * width)
                    py = int(ny * height)
                    await input_shell.tap(px, py)

                elif ev_type == "swipe":
                    nx1, ny1 = float(ev.get("x1", 0)), float(ev.get("y1", 0))
                    nx2, ny2 = float(ev.get("x2", 0)), float(ev.get("y2", 0))
                    px1, py1 = int(nx1 * width), int(ny1 * height)
                    px2, py2 = int(nx2 * width), int(ny2 * height)
                    duration = int(ev.get("duration", 300))
                    await input_shell.swipe(px1, py1, px2, py2, duration)

                elif ev_type == "keyevent":
                    code = int(ev.get("code", 0))
                    await input_shell.keyevent(code)

                elif ev_type == "text":
                    t = ev.get("text", "")
                    if t:
                        await input_shell.text(t)

                elif ev_type == "refresh":
                    logger.debug("Mirror manual refresh for %s", serial)
                    png = await _screencap_execout(serial)
                    if png:
                        try:
                            await websocket.send_bytes(png)
                        except Exception:
                            break
                    else:
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "message": "Manual refresh failed",
                            }))
                        except Exception:
                            break

        except (WebSocketDisconnect, Exception) as exc:
            logger.debug("mirror input_loop ended: %s", exc)

    frame_task = asyncio.create_task(frame_loop())
    input_task = asyncio.create_task(input_loop())

    try:
        done, pending = await asyncio.wait(
            [frame_task, input_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    finally:
        stop_event.set()
        await input_shell.close()
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Mirror closed: serial=%s user=%s", serial, display_name)
