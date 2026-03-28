"""CI/CD integration router — API-key auth, no browser session required.

Endpoint prefix: /api/ci/

Authentication: every request must supply the CI API key either as:
  • JSON body field  "api_key": "..."
  • HTTP header      X-API-Key: ...

Reservations made through this API are stored in the same reservations table
as human reservations; the reserved_by value is "[CI] <job_name>" so the web
UI can display them distinctly.
"""

import asyncio
import logging
import re
import shlex
import subprocess
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from .config import ADB_LAB_CI_API_KEY, ADB_LAB_SERVER_IP, ADB_PATH
from .database import get_db
from .devices import get_devices
from .reservations import _do_release, get_active_reservation

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ci", tags=["ci"])


# ── Android version → API level lookup ────────────────────────────────────────

_VERSION_TO_API: dict[str, int] = {
    "5.0": 21, "5.1": 22,
    "6.0": 23,
    "7.0": 24, "7.1": 25,
    "8.0": 26, "8.1": 27,
    "9": 28, "10": 29, "11": 30,
    "12": 31, "12.1": 32,
    "13": 33, "14": 34, "15": 35,
}


def _api_level(android_version: str) -> int:
    v = android_version.strip()
    if v in _VERSION_TO_API:
        return _VERSION_TO_API[v]
    major = v.split(".")[0]
    # Try any key whose major version matches
    for key, api in _VERSION_TO_API.items():
        if key.split(".")[0] == major:
            return api
    try:
        return int(major) + 19   # rough future-version estimate
    except ValueError:
        return 0


# ── Auth ───────────────────────────────────────────────────────────────────────

def _verify_key(key: Optional[str]) -> None:
    if not key or key != ADB_LAB_CI_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing CI API key")


# ── Command safety ─────────────────────────────────────────────────────────────

# adb subcommands that must never be invoked via the API
_BLOCKED_ADB_SUBCMDS = frozenset({
    "reboot",        # reboots device mid-session
    "kill-server",   # would kill the shared adb server for ALL users
    "emu",           # emulator control — not applicable and risky
})

# shell commands (first token after "adb -s X shell ...") that are blocked
_BLOCKED_SHELL_CMDS = frozenset({
    "reboot", "halt", "poweroff", "shutdown",
})

# Regex patterns in the full shell command string that indicate dangerous ops
_BLOCKED_SHELL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\b.*-[a-zA-Z]*r[a-zA-Z]*\s+/",  re.I), "recursive delete from /"),
    (re.compile(r"\bmkfs\b",                             re.I), "filesystem format"),
    (re.compile(r"\bdd\b.+\bof=/dev/",                  re.I), "write to block device"),
    (re.compile(r"\bwipe\b",                             re.I), "partition wipe"),
    (re.compile(r">\s*/dev/block",                       re.I), "redirect to block device"),
]


def _check_command(command: str) -> None:
    """Raise HTTP 400 if the command is on the blocklist."""
    command = command.strip()
    if not command:
        raise HTTPException(status_code=400, detail="command is empty")

    try:
        parts = shlex.split(command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid command syntax: {exc}")

    if not parts:
        raise HTTPException(status_code=400, detail="command is empty")

    subcommand = parts[0].lower()

    if subcommand in _BLOCKED_ADB_SUBCMDS:
        raise HTTPException(
            status_code=400,
            detail=f"adb subcommand '{subcommand}' is not permitted via the CI API",
        )

    if subcommand == "shell" and len(parts) > 1:
        shell_first = parts[1].lower()
        if shell_first in _BLOCKED_SHELL_CMDS:
            raise HTTPException(
                status_code=400,
                detail=f"shell command '{shell_first}' is not permitted via the CI API",
            )
        shell_str = " ".join(parts[1:])
        for pattern, desc in _BLOCKED_SHELL_PATTERNS:
            if pattern.search(shell_str):
                raise HTTPException(
                    status_code=400,
                    detail=f"Blocked dangerous operation: {desc}",
                )


# ── Device filter ──────────────────────────────────────────────────────────────

def _matches_filter(device: dict, filt: Any) -> bool:
    """Return True if device info satisfies the filter."""
    if filt is None or filt == "any":
        return True
    if not isinstance(filt, dict):
        return True  # unknown shape → don't filter out

    if "min_api" in filt:
        if _api_level(device.get("android_version", "0")) < int(filt["min_api"]):
            return False

    if "model" in filt:
        needle = str(filt["model"]).lower()
        haystack = (
            device.get("model", "") + " " + device.get("manufacturer", "")
        ).lower()
        if needle not in haystack:
            return False

    if "manufacturer" in filt:
        needle = str(filt["manufacturer"]).lower()
        if needle not in device.get("manufacturer", "").lower():
            return False

    if "android_version" in filt:
        if device.get("android_version", "") != str(filt["android_version"]):
            return False

    return True


# ── Pydantic models ────────────────────────────────────────────────────────────

class CIReserveRequest(BaseModel):
    api_key: Optional[str] = None
    job_name: str
    # Specify one of: device_serial (a real serial or "any") or device_filter
    device_serial: Optional[str] = None
    device_filter: Optional[Any] = None   # str "any" | dict of criteria


class CIReleaseRequest(BaseModel):
    api_key: Optional[str] = None
    job_name: Optional[str] = None   # if provided, ownership is verified
    serial: str


class CIExecuteRequest(BaseModel):
    api_key: Optional[str] = None
    job_name: Optional[str] = None   # if provided, ownership is verified
    serial: str
    command: str    # adb subcommand + args, e.g. "shell pm list packages"
    timeout: int = 60   # seconds, clamped to 1-300


# ── Internal helpers ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


def _ci_reserved_by(job_name: str) -> str:
    return f"[CI] {job_name}"


def _adb_connect_cmd(serial: str) -> str:
    return f"adb -H {ADB_LAB_SERVER_IP} -P 5037 -s {serial} shell"


async def _insert_reservation(serial: str, job_name: str) -> str:
    """Write a CI reservation row; return reserved_at ISO string."""
    now = _now_iso()
    reserved_by = _ci_reserved_by(job_name)
    async with get_db() as db:
        await db.execute(
            "INSERT INTO reservations (device_serial, reserved_by, reserved_at)"
            " VALUES (?, ?, ?)",
            (serial, reserved_by, now),
        )
        await db.commit()
    logger.info("CI reserved %s for job '%s'", serial, job_name)
    return now


def _run_adb(cmd: list[str], timeout: int) -> tuple[int, str, str]:
    """Blocking subprocess call — run via run_in_executor."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", f"adb binary not found: {cmd[0]}"
    except Exception as exc:
        return -1, "", str(exc)


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("/reserve")
async def ci_reserve(
    body: CIReserveRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Reserve a device for a CI pipeline.

    Specify either:
    - device_serial: a specific serial number, or "any"
    - device_filter: {"min_api": 30} | {"model": "Pixel 6"} | {"manufacturer": "Google"}

    Returns serial, model info, and an adb_connect_command ready to paste into
    a Jenkinsfile `environment {}` block.
    """
    _verify_key(body.api_key or x_api_key)

    devices = get_devices()
    target: Optional[str] = None

    want_specific = (
        body.device_serial
        and body.device_serial.lower() != "any"
    )

    if want_specific:
        target = body.device_serial
        if target not in devices:
            raise HTTPException(
                status_code=404,
                detail=f"Device '{target}' is not online",
            )
        existing = await get_active_reservation(target)
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Device '{target}' is already reserved by {existing['reserved_by']}",
            )
    else:
        # Auto-select: first online device matching filter
        filt = body.device_filter
        if filt is None:
            filt = "any"

        for serial, info in devices.items():
            if not _matches_filter(info, filt):
                continue
            existing = await get_active_reservation(serial)
            if existing is None:
                target = serial
                break

        if target is None:
            raise HTTPException(
                status_code=503,
                detail="No available device matches the requested filter",
            )

    reserved_at = await _insert_reservation(target, body.job_name)
    dev = devices[target]

    return {
        "ok": True,
        "serial": target,
        "model": dev.get("model", "Unknown"),
        "manufacturer": dev.get("manufacturer", "Unknown"),
        "android_version": dev.get("android_version", "Unknown"),
        "android_api_level": _api_level(dev.get("android_version", "0")),
        "battery_level": dev.get("battery_level"),
        "reserved_at": reserved_at,
        "adb_connect_command": _adb_connect_cmd(target),
        # Convenience block for Jenkinsfile environment{}
        "env": {
            "ANDROID_ADB_SERVER_ADDRESS": ADB_LAB_SERVER_IP,
            "ANDROID_ADB_SERVER_PORT": "5037",
            "ANDROID_SERIAL": target,
        },
    }


@router.post("/release")
async def ci_release(
    body: CIReleaseRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Release a CI reservation. Call this in your pipeline's post { always {} } block."""
    _verify_key(body.api_key or x_api_key)

    existing = await get_active_reservation(body.serial)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Device '{body.serial}' is not reserved")

    reserved_by = existing["reserved_by"]

    if not reserved_by.startswith("[CI] "):
        raise HTTPException(
            status_code=403,
            detail=f"Device is reserved by a human user ({reserved_by}); use the web UI to release it",
        )

    # Optional: verify the releasing job actually owns the reservation
    if body.job_name and reserved_by != _ci_reserved_by(body.job_name):
        raise HTTPException(
            status_code=403,
            detail=f"Device is reserved by job '{reserved_by}', not '{body.job_name}'",
        )

    now = _now_iso()
    await _do_release(body.serial, now)
    logger.info("CI released %s (was: %s)", body.serial, reserved_by)
    return {"ok": True, "serial": body.serial, "released_at": now}


@router.api_route("/devices", methods=["GET", "POST"])
async def ci_devices(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """List all devices with current availability status.

    Useful at the start of a pipeline to decide which device to target, or
    to poll until a device becomes available.

    Authenticate via X-API-Key header.
    """
    _verify_key(x_api_key)

    devices = get_devices()
    all_reservations: dict[str, dict] = {}

    # Fetch all active reservations in one pass
    from .reservations import get_all_active_reservations
    all_reservations = await get_all_active_reservations()

    result = []
    for serial, info in devices.items():
        res = all_reservations.get(serial)
        entry: dict[str, Any] = {
            "serial": serial,
            "model": info.get("model", "Unknown"),
            "manufacturer": info.get("manufacturer", "Unknown"),
            "android_version": info.get("android_version", "Unknown"),
            "android_api_level": _api_level(info.get("android_version", "0")),
            "battery_level": info.get("battery_level"),
            "online": True,
            "available": res is None,
        }
        if res:
            entry["reserved_by"] = res["reserved_by"]
            entry["reserved_at"] = res["reserved_at"]
            entry["is_ci_reservation"] = res["reserved_by"].startswith("[CI] ")
        result.append(entry)

    return {
        "ok": True,
        "server_ip": ADB_LAB_SERVER_IP,
        "adb_port": 5037,
        "device_count": len(result),
        "available_count": sum(1 for d in result if d["available"]),
        "devices": result,
    }


@router.post("/execute")
async def ci_execute(
    body: CIExecuteRequest,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    """Run an adb command on a reserved device and return stdout/stderr/exit_code.

    The command is the adb subcommand + arguments WITHOUT 'adb -s <serial>'.
    Examples:
      "shell pm list packages"
      "shell am instrument -w com.example/androidx.test.runner.AndroidJUnitRunner"
      "install /tmp/app-debug.apk"
      "pull /sdcard/screenshots/ /tmp/"

    Dangerous commands (reboot, kill-server, rm -rf /, mkfs, dd) are blocked.
    Timeout is clamped to 1-300 seconds.
    """
    _verify_key(body.api_key or x_api_key)
    _check_command(body.command)

    # Verify device is reserved (by any CI job — or optionally by THIS job)
    existing = await get_active_reservation(body.serial)
    if not existing:
        raise HTTPException(
            status_code=403,
            detail=f"Device '{body.serial}' is not reserved. Reserve it first with POST /api/ci/reserve",
        )

    reserved_by = existing["reserved_by"]

    if not reserved_by.startswith("[CI] "):
        raise HTTPException(
            status_code=403,
            detail=f"Device is reserved by a human user ({reserved_by}), not a CI job",
        )

    # Optional strict ownership check
    if body.job_name and reserved_by != _ci_reserved_by(body.job_name):
        raise HTTPException(
            status_code=403,
            detail=f"Device is reserved by '{reserved_by}', not your job '{body.job_name}'",
        )

    try:
        cmd_parts = shlex.split(body.command)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid command: {exc}")

    full_cmd = [ADB_PATH, "-s", body.serial] + cmd_parts
    timeout = max(1, min(body.timeout, 300))

    logger.info("CI execute [%s] %s", body.serial, body.command)

    loop = asyncio.get_running_loop()
    rc, stdout, stderr = await loop.run_in_executor(
        None, lambda: _run_adb(full_cmd, timeout)
    )

    return {
        "ok": rc == 0,
        "exit_code": rc,
        "stdout": stdout,
        "stderr": stderr,
        "serial": body.serial,
        "command": body.command,
    }
