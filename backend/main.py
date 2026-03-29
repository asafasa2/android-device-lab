import asyncio
import json
import logging
import os
import socket
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .auth import router as auth_router, get_session
from .config import ADB_PATH, ADB_LAB_SERVER_IP, RESERVATION_TIMEOUT_HOURS, SSH_USERNAME
from .database import init_db
from .devices import get_devices, start_polling, stop_polling, router as devices_router
from .reservations import (
    router as reservations_router,
    get_all_active_reservations,
    start_auto_release,
    stop_auto_release,
)
from .terminal import router as terminal_router
from .ci import router as ci_router
from .mirror import router as mirror_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    start_polling()
    start_auto_release()
    logger.info("ADB Lab server started")
    try:
        yield
    finally:
        stop_polling()
        stop_auto_release()
        _kill_adb_server()
        logger.info("ADB Lab server stopped")


def _kill_adb_server():
    try:
        subprocess.run([ADB_PATH, "kill-server"], timeout=5, capture_output=True)
        logger.info("adb server killed")
    except Exception as e:
        logger.warning("Could not kill adb server: %s", e)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="ADB Device Lab", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth middleware ───────────────────────────────────────────────────────────

UNPROTECTED = {"/api/login", "/api/status", "/", "/favicon.ico"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Allow unprotected paths and static assets
    # CI routes use their own API-key auth — skip cookie check for them
    if (
        path in UNPROTECTED
        or path.startswith("/api/ci/")
        or path.startswith("/assets/")
        or path.startswith("/static/")
        or path.startswith("/ws/")
        or not path.startswith("/api/")
    ):
        return await call_next(request)

    session = get_session(request)
    if not session:
        return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
    return await call_next(request)


# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(auth_router)
app.include_router(devices_router)
app.include_router(reservations_router)
app.include_router(terminal_router)
app.include_router(ci_router)
app.include_router(mirror_router)


# ── REST endpoints ────────────────────────────────────────────────────────────

@app.get("/api/status")
async def status():
    devices = get_devices()
    return {
        "ok": True,
        "hostname": socket.gethostname(),
        "server_ip": ADB_LAB_SERVER_IP,
        "ssh_username": SSH_USERNAME,
        "device_count": len(devices),
        "reservation_timeout_hours": RESERVATION_TIMEOUT_HOURS,
    }


@app.get("/api/devices")
async def list_devices(request: Request):
    devices = get_devices()
    reservations = await get_all_active_reservations()

    result = []
    for serial, info in devices.items():
        entry = dict(info)
        res = reservations.get(serial)
        if res:
            entry["reservation"] = {
                "reserved_by": res["reserved_by"],
                "reserved_at": res["reserved_at"],
            }
        else:
            entry["reservation"] = None
        result.append(entry)

    # Also include reserved-but-offline devices
    offline_reserved = {
        s: r for s, r in reservations.items() if s not in devices
    }
    for serial, res in offline_reserved.items():
        result.append({
            "serial": serial,
            "model": "Unknown",
            "manufacturer": "Unknown",
            "device_name": serial,
            "android_version": "Unknown",
            "battery_level": None,
            "last_seen": None,
            "online": False,
            "reservation": {
                "reserved_by": res["reserved_by"],
                "reserved_at": res["reserved_at"],
            },
        })

    return {"devices": result}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/devices")
async def ws_devices(websocket: WebSocket):
    # Auth check via cookie
    session = get_session(websocket)
    if not session:
        await websocket.close(code=1008)  # Policy Violation
        return

    await websocket.accept()
    logger.info("WS client connected: %s", session["display_name"])

    try:
        while True:
            devices = get_devices()
            reservations = await get_all_active_reservations()

            payload = []
            for serial, info in devices.items():
                entry = dict(info)
                res = reservations.get(serial)
                entry["reservation"] = (
                    {"reserved_by": res["reserved_by"], "reserved_at": res["reserved_at"]}
                    if res else None
                )
                payload.append(entry)

            await websocket.send_text(json.dumps({"devices": payload}))
            await asyncio.sleep(3)

    except WebSocketDisconnect:
        logger.info("WS client disconnected: %s", session["display_name"])
    except Exception as e:
        logger.error("WS error: %s", e)


# ── Static files (frontend) ───────────────────────────────────────────────────

_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")
