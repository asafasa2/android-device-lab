import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, HTTPException

from .auth import require_auth
from .config import RESERVATION_TIMEOUT_HOURS
from .database import get_db

logger = logging.getLogger(__name__)

router = APIRouter()

_auto_release_task: Optional[asyncio.Task] = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.utcnow().isoformat()


async def get_active_reservation(serial: str) -> Optional[dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM reservations WHERE device_serial = ? AND released_at IS NULL",
            (serial,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_all_active_reservations() -> dict[str, dict]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM reservations WHERE released_at IS NULL"
        ) as cur:
            rows = await cur.fetchall()
            return {row["device_serial"]: dict(row) for row in rows}


async def _do_release(serial: str, released_at: str):
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM reservations WHERE device_serial = ? AND released_at IS NULL",
            (serial,),
        ) as cur:
            row = await cur.fetchone()

        if row:
            await db.execute(
                """INSERT INTO reservation_history
                   (device_serial, reserved_by, reserved_at, released_at)
                   VALUES (?, ?, ?, ?)""",
                (row["device_serial"], row["reserved_by"], row["reserved_at"], released_at),
            )
            await db.execute(
                "DELETE FROM reservations WHERE device_serial = ?",
                (serial,),
            )
            await db.commit()


# ── Auto-release background task ─────────────────────────────────────────────

async def _auto_release_loop():
    while True:
        try:
            await _check_timeouts()
        except Exception as e:
            logger.error("Auto-release error: %s", e)
        await asyncio.sleep(60)


async def _check_timeouts():
    cutoff = (
        datetime.utcnow() - timedelta(hours=RESERVATION_TIMEOUT_HOURS)
    ).isoformat()

    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM reservations WHERE released_at IS NULL AND reserved_at < ?",
            (cutoff,),
        ) as cur:
            expired = [dict(r) for r in await cur.fetchall()]

    for r in expired:
        now = _now_iso()
        logger.info(
            "Auto-releasing %s (reserved by %s at %s)",
            r["device_serial"], r["reserved_by"], r["reserved_at"],
        )
        await _do_release(r["device_serial"], now)


def start_auto_release():
    global _auto_release_task
    _auto_release_task = asyncio.create_task(_auto_release_loop())
    logger.info(
        "Auto-release task started (timeout=%.1fh)", RESERVATION_TIMEOUT_HOURS
    )


def stop_auto_release():
    global _auto_release_task
    if _auto_release_task:
        _auto_release_task.cancel()
        _auto_release_task = None


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/api/devices/{serial}/reserve")
async def reserve_device(serial: str, request: Request):
    session = require_auth(request)
    display_name = session["display_name"]

    existing = await get_active_reservation(serial)
    if existing:
        if existing["reserved_by"] == display_name:
            return {"ok": True, "message": "Already reserved by you", "reservation": existing}
        raise HTTPException(
            status_code=409,
            detail=f"Device already reserved by {existing['reserved_by']}",
        )

    now = _now_iso()
    async with get_db() as db:
        await db.execute(
            "INSERT INTO reservations (device_serial, reserved_by, reserved_at) VALUES (?, ?, ?)",
            (serial, display_name, now),
        )
        await db.commit()

    logger.info("Reserved %s for %s", serial, display_name)
    return {
        "ok": True,
        "reservation": {
            "device_serial": serial,
            "reserved_by": display_name,
            "reserved_at": now,
            "released_at": None,
        },
    }


@router.post("/api/devices/{serial}/release")
async def release_device(serial: str, request: Request):
    session = require_auth(request)
    display_name = session["display_name"]

    existing = await get_active_reservation(serial)
    if not existing:
        raise HTTPException(status_code=404, detail="Device is not reserved")
    if existing["reserved_by"] != display_name:
        raise HTTPException(
            status_code=403,
            detail=f"Device is reserved by {existing['reserved_by']}, not you",
        )

    now = _now_iso()
    await _do_release(serial, now)
    logger.info("Released %s by %s", serial, display_name)
    return {"ok": True}


@router.get("/api/devices/{serial}/history")
async def device_history(serial: str, request: Request):
    require_auth(request)
    async with get_db() as db:
        async with db.execute(
            """SELECT * FROM reservation_history
               WHERE device_serial = ?
               ORDER BY reserved_at DESC LIMIT 50""",
            (serial,),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
    return {"history": rows}
