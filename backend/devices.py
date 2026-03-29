import asyncio
import logging
import re
import subprocess
from datetime import datetime
from typing import Optional

from .config import ADB_PATH, DEVICE_POLL_INTERVAL

logger = logging.getLogger(__name__)

# serial -> device info dict
_devices: dict[str, dict] = {}
_poll_task: Optional[asyncio.Task] = None


def get_devices() -> dict[str, dict]:
    return dict(_devices)


def _run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.warning("ADB command timed out: %s", cmd)
        return -1, "", "timeout"
    except FileNotFoundError:
        logger.error("adb binary not found at: %s", ADB_PATH)
        return -1, "", "adb not found"
    except Exception as e:
        logger.error("ADB command error %s: %s", cmd, e)
        return -1, "", str(e)


def ensure_adb_server():
    """Kill any existing adb server and restart it listening on all interfaces.

    We always kill first: if the server was previously started without -a (the
    default, which binds to 127.0.0.1 only), adb start-server will detect it
    alive and skip re-launching — leaving the port inaccessible from the LAN.
    """
    logger.info("Restarting adb server to listen on 0.0.0.0:5037")
    _run([ADB_PATH, "kill-server"])
    rc, out, err = _run([ADB_PATH, "-a", "-P", "5037", "start-server"])
    if rc != 0:
        logger.warning("adb start-server returned %d: %s", rc, err)
    else:
        logger.info("adb server started on 0.0.0.0:5037")


def _parse_devices(output: str) -> list[str]:
    """Parse `adb devices -l` output and return list of online serials."""
    serials = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state == "device":
            serials.append(serial)
        elif state in ("offline", "unauthorized"):
            logger.debug("Device %s is %s — skipping", serial, state)
    return serials


def _get_props(serial: str) -> dict:
    """Fetch relevant system properties for a device."""
    rc, out, err = _run([ADB_PATH, "-s", serial, "shell", "getprop"])
    props = {}
    if rc != 0:
        return props
    for line in out.splitlines():
        m = re.match(r"\[(.+?)\]:\s*\[(.*)?\]", line)
        if m:
            props[m.group(1)] = m.group(2)
    return props


def _get_battery(serial: str) -> Optional[int]:
    """Return battery level (0-100) or None."""
    rc, out, _ = _run([ADB_PATH, "-s", serial, "shell", "dumpsys", "battery"])
    if rc != 0:
        return None
    for line in out.splitlines():
        m = re.search(r"level:\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def _build_device_info(serial: str) -> dict:
    props = _get_props(serial)
    battery = _get_battery(serial)

    model = (
        props.get("ro.product.model")
        or props.get("ro.product.name")
        or "Unknown"
    )
    manufacturer = (
        props.get("ro.product.manufacturer")
        or props.get("ro.product.brand")
        or "Unknown"
    )
    android_version = (
        props.get("ro.build.version.release") or "Unknown"
    )
    device_name = props.get("ro.product.device") or serial

    return {
        "serial": serial,
        "model": model,
        "manufacturer": manufacturer,
        "device_name": device_name,
        "android_version": android_version,
        "battery_level": battery,
        "last_seen": datetime.utcnow().isoformat(),
        "online": True,
    }


async def _poll_loop():
    while True:
        try:
            await _do_poll()
        except Exception as e:
            logger.error("Device poll error: %s", e)
        await asyncio.sleep(DEVICE_POLL_INTERVAL)


async def _do_poll():
    loop = asyncio.get_event_loop()

    rc, out, err = await loop.run_in_executor(
        None, lambda: _run([ADB_PATH, "devices", "-l"])
    )
    if rc != 0:
        logger.warning("adb devices failed: %s", err)
        return

    online_serials = set(_parse_devices(out))
    current_serials = set(_devices.keys())

    # Mark disconnected devices as offline and remove them
    for gone in current_serials - online_serials:
        logger.info("Device disconnected: %s", gone)
        del _devices[gone]

    # New devices
    for new in online_serials - current_serials:
        logger.info("Device connected: %s", new)
        info = await loop.run_in_executor(None, lambda s=new: _build_device_info(s))
        _devices[new] = info

    # Refresh existing devices (battery, etc.)
    for serial in online_serials & current_serials:
        battery = await loop.run_in_executor(
            None, lambda s=serial: _get_battery(s)
        )
        _devices[serial]["battery_level"] = battery
        _devices[serial]["last_seen"] = datetime.utcnow().isoformat()


def start_polling():
    global _poll_task
    ensure_adb_server()
    _poll_task = asyncio.create_task(_poll_loop())
    logger.info("Device polling started (interval=%ds)", DEVICE_POLL_INTERVAL)


def stop_polling():
    global _poll_task
    if _poll_task:
        _poll_task.cancel()
        _poll_task = None
    logger.info("Device polling stopped")
