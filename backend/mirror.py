import asyncio
import json
import logging
import re
import subprocess

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import get_session
from .config import ADB_PATH
from .devices import get_devices
from .reservations import get_active_reservation

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_screen_size(serial: str) -> tuple[int, int]:
    """Return (width, height) from `adb shell wm size`. Falls back to 1080x1920."""
    try:
        result = subprocess.run(
            [ADB_PATH, "-s", serial, "shell", "wm", "size"],
            capture_output=True, text=True, timeout=5,
        )
        m = re.search(r"(\d+)x(\d+)", result.stdout)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception as e:
        logger.warning("wm size failed for %s: %s", serial, e)
    return 1080, 1920


async def _screencap(serial: str) -> bytes | None:
    """Run `adb exec-out screencap -p` and return PNG bytes, or None on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH, "-s", serial, "exec-out", "screencap", "-p",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        if proc.returncode == 0 and stdout:
            return stdout
    except asyncio.TimeoutError:
        logger.warning("screencap timed out for %s", serial)
    except Exception as e:
        logger.warning("screencap error for %s: %s", serial, e)
    return None


async def _adb_input(serial: str, *args: str) -> None:
    """Fire-and-forget adb input command."""
    try:
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH, "-s", serial, "shell", "input", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception as e:
        logger.debug("adb input error for %s: %s", serial, e)


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

    # Get screen dimensions once
    width, height = _get_screen_size(serial)
    await websocket.send_text(json.dumps({
        "type": "info",
        "width": width,
        "height": height,
        "serial": serial,
    }))

    # ── Task: send frames every 500 ms ───────────────────────────────────────
    stop_event = asyncio.Event()

    async def frame_loop() -> None:
        while not stop_event.is_set():
            frame_start = asyncio.get_event_loop().time()
            png = await _screencap(serial)
            if stop_event.is_set():
                break
            try:
                if png:
                    await websocket.send_bytes(png)
                else:
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": "screencap failed",
                    }))
            except Exception:
                break
            elapsed = asyncio.get_event_loop().time() - frame_start
            sleep_for = max(0.0, 0.5 - elapsed)
            try:
                await asyncio.wait_for(
                    asyncio.shield(asyncio.sleep(sleep_for)),
                    timeout=sleep_for + 0.1,
                )
            except (asyncio.TimeoutError, asyncio.CancelledError):
                break

    # ── Task: receive input events ────────────────────────────────────────────
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
                    x = float(ev.get("x", 0))
                    y = float(ev.get("y", 0))
                    px = int(x * width)
                    py = int(y * height)
                    asyncio.create_task(_adb_input(serial, "tap", str(px), str(py)))

                elif ev_type == "swipe":
                    x1 = int(float(ev.get("x1", 0)) * width)
                    y1 = int(float(ev.get("y1", 0)) * height)
                    x2 = int(float(ev.get("x2", 0)) * width)
                    y2 = int(float(ev.get("y2", 0)) * height)
                    duration = int(ev.get("duration", 300))
                    asyncio.create_task(_adb_input(
                        serial, "swipe",
                        str(x1), str(y1), str(x2), str(y2), str(duration),
                    ))

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
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Mirror closed: serial=%s user=%s", serial, display_name)
