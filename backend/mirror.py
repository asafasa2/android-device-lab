import asyncio
import json
import logging
import os
import re
import subprocess
import tempfile
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import get_session
from .config import ADB_PATH
from .devices import get_devices
from .reservations import get_active_reservation

logger = logging.getLogger(__name__)
router = APIRouter()

PNG_MAGIC = b"\x89PNG"
FRAME_INTERVAL = 1.0        # seconds between frames (1 FPS)
SCREENCAP_TIMEOUT = 5.0     # seconds before giving up on a single screencap
CONSECUTIVE_FAIL_LIMIT = 3  # switch to fallback method after this many failures


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
        # Look for SurfaceOrientation
        m = re.search(r"SurfaceOrientation:\s*(\d)", result.stdout)
        if m:
            orientation = int(m.group(1))
            logger.debug("Device %s orientation: %d", serial, orientation)
            return orientation
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
    return True  # assume on if check fails


async def _screencap_execout(serial: str) -> bytes | None:
    """Primary method: `adb exec-out screencap -p` and return PNG bytes."""
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH, "-s", serial, "exec-out", "screencap", "-p",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SCREENCAP_TIMEOUT)
        elapsed = time.monotonic() - t0

        if proc.returncode != 0:
            logger.warning("screencap exec-out failed for %s: rc=%d stderr=%r (%.1fs)",
                           serial, proc.returncode, stderr.decode(errors="replace")[:200], elapsed)
            return None

        if not stdout or len(stdout) < 8:
            logger.warning("screencap exec-out returned empty data for %s (%.1fs)", serial, elapsed)
            return None

        # Validate PNG magic bytes
        if not stdout[:4].startswith(PNG_MAGIC[:4]):
            logger.warning("screencap exec-out returned non-PNG data for %s: "
                           "first 8 bytes=%r (%.1fs)", serial, stdout[:8], elapsed)
            return None

        logger.debug("screencap exec-out OK for %s: %d bytes (%.1fs)",
                      serial, len(stdout), elapsed)
        return stdout

    except asyncio.TimeoutError:
        logger.warning("screencap exec-out timed out for %s (>%.0fs)", serial, SCREENCAP_TIMEOUT)
        return None
    except Exception as e:
        logger.warning("screencap exec-out error for %s: %s", serial, e)
        return None


async def _screencap_pull(serial: str) -> bytes | None:
    """Fallback method: screencap to file on device, then pull it."""
    t0 = time.monotonic()
    remote_path = "/sdcard/adb_lab_screen.png"

    try:
        # Step 1: capture to file on device
        proc = await asyncio.create_subprocess_exec(
            ADB_PATH, "-s", serial, "shell", "screencap", "-p", remote_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=SCREENCAP_TIMEOUT)
        if proc.returncode != 0:
            logger.warning("screencap-to-file failed for %s (rc=%d)", serial, proc.returncode)
            return None

        # Step 2: pull the file
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            local_path = tmp.name

        try:
            proc = await asyncio.create_subprocess_exec(
                ADB_PATH, "-s", serial, "pull", remote_path, local_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=SCREENCAP_TIMEOUT)
            if proc.returncode != 0:
                logger.warning("adb pull screencap failed for %s (rc=%d)", serial, proc.returncode)
                return None

            with open(local_path, "rb") as f:
                data = f.read()

            elapsed = time.monotonic() - t0
            if data and data[:4].startswith(PNG_MAGIC[:4]):
                logger.debug("screencap pull OK for %s: %d bytes (%.1fs)",
                              serial, len(data), elapsed)
                return data
            else:
                logger.warning("screencap pull returned non-PNG for %s (%.1fs)", serial, elapsed)
                return None
        finally:
            try:
                os.unlink(local_path)
            except OSError:
                pass
            # Cleanup remote file (fire and forget)
            try:
                await asyncio.create_subprocess_exec(
                    ADB_PATH, "-s", serial, "shell", "rm", "-f", remote_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception:
                pass

    except asyncio.TimeoutError:
        logger.warning("screencap pull timed out for %s (>%.0fs)", serial, SCREENCAP_TIMEOUT)
        return None
    except Exception as e:
        logger.warning("screencap pull error for %s: %s", serial, e)
        return None


async def _adb_input(serial: str, *args: str) -> bool:
    """Run an adb input command and return True on success."""
    cmd = [ADB_PATH, "-s", serial, "shell", "input", *args]
    logger.info("adb input: %s", " ".join(cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        if proc.returncode != 0:
            logger.warning("adb input failed for %s: rc=%d stdout=%r stderr=%r",
                           serial, proc.returncode,
                           stdout.decode(errors="replace")[:200],
                           stderr.decode(errors="replace")[:200])
            return False
        logger.debug("adb input OK for %s: %s", serial, " ".join(args))
        return True
    except asyncio.TimeoutError:
        logger.warning("adb input timed out for %s: %s", serial, " ".join(args))
        return False
    except Exception as e:
        logger.warning("adb input error for %s: %s", serial, e)
        return False


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

    # wm size returns physical dimensions (always portrait-oriented)
    # If device is in landscape (orientation 1 or 3), swap for display coords
    if orientation in (1, 3):
        width, height = phys_h, phys_w
    else:
        width, height = phys_w, phys_h

    logger.info("Mirror for %s: physical=%dx%d orientation=%d display=%dx%d",
                serial, phys_w, phys_h, orientation, width, height)

    await websocket.send_text(json.dumps({
        "type": "info",
        "width": width,
        "height": height,
        "serial": serial,
    }))

    # ── Task: send frames ──────────────────────────────────────────────────────
    stop_event = asyncio.Event()
    use_pull_fallback = False
    consecutive_fails = 0

    async def frame_loop() -> None:
        nonlocal use_pull_fallback, consecutive_fails

        while not stop_event.is_set():
            frame_start = asyncio.get_event_loop().time()

            # Choose screencap method
            if use_pull_fallback:
                png = await _screencap_pull(serial)
            else:
                png = await _screencap_execout(serial)

            if stop_event.is_set():
                break

            try:
                if png:
                    consecutive_fails = 0
                    await websocket.send_bytes(png)
                else:
                    consecutive_fails += 1
                    logger.warning("Mirror screencap fail #%d for %s (method=%s)",
                                   consecutive_fails, serial,
                                   "pull" if use_pull_fallback else "exec-out")

                    # Switch to pull fallback after N consecutive exec-out failures
                    if not use_pull_fallback and consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                        logger.info("Mirror switching to pull fallback for %s", serial)
                        use_pull_fallback = True
                        consecutive_fails = 0
                        await websocket.send_text(json.dumps({
                            "type": "status",
                            "message": "Switching to fallback capture method…",
                        }))
                        continue

                    # Check if screen is off
                    if consecutive_fails >= 2:
                        screen_on = await _check_screen_on(serial)
                        if not screen_on:
                            await websocket.send_text(json.dumps({
                                "type": "screen_off",
                                "message": "Device screen is off — tap to wake",
                            }))

                    # If both methods fail repeatedly, send error
                    if use_pull_fallback and consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": "screencap failed — device may not support screen capture",
                        }))
            except Exception:
                break

            elapsed = asyncio.get_event_loop().time() - frame_start
            sleep_for = max(0.0, FRAME_INTERVAL - elapsed)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # normal — sleep expired, loop continues

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
                    nx = float(ev.get("x", 0))
                    ny = float(ev.get("y", 0))
                    px = int(nx * width)
                    py = int(ny * height)
                    logger.info("Mirror tap for %s: norm=(%.3f, %.3f) → px=(%d, %d) "
                                "display=%dx%d", serial, nx, ny, px, py, width, height)
                    await _adb_input(serial, "tap", str(px), str(py))

                elif ev_type == "swipe":
                    nx1, ny1 = float(ev.get("x1", 0)), float(ev.get("y1", 0))
                    nx2, ny2 = float(ev.get("x2", 0)), float(ev.get("y2", 0))
                    px1, py1 = int(nx1 * width), int(ny1 * height)
                    px2, py2 = int(nx2 * width), int(ny2 * height)
                    duration = int(ev.get("duration", 300))
                    logger.info("Mirror swipe for %s: (%d,%d)→(%d,%d) %dms "
                                "display=%dx%d", serial, px1, py1, px2, py2,
                                duration, width, height)
                    await _adb_input(
                        serial, "swipe",
                        str(px1), str(py1), str(px2), str(py2), str(duration),
                    )

                elif ev_type == "refresh":
                    logger.debug("Mirror manual refresh for %s", serial)
                    png = await _screencap_execout(serial)
                    if not png and use_pull_fallback:
                        png = await _screencap_pull(serial)
                    if png:
                        try:
                            await websocket.send_bytes(png)
                        except Exception:
                            break
                    else:
                        try:
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "message": "Manual refresh failed — screencap error",
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
        try:
            await websocket.close()
        except Exception:
            pass
        logger.info("Mirror closed: serial=%s user=%s", serial, display_name)
