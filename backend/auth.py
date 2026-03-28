import secrets
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import ADB_LAB_PASSWORD

logger = logging.getLogger(__name__)

router = APIRouter()

# In-memory session store: token -> {display_name, created_at}
_sessions: dict[str, dict] = {}

SESSION_TTL_HOURS = 24


class LoginRequest(BaseModel):
    password: str
    display_name: str


def _purge_expired():
    cutoff = datetime.utcnow() - timedelta(hours=SESSION_TTL_HOURS)
    expired = [t for t, s in _sessions.items() if s["created_at"] < cutoff]
    for t in expired:
        del _sessions[t]


def get_session(request: Request) -> Optional[dict]:
    token = request.cookies.get("adb_lab_session")
    if not token:
        return None
    session = _sessions.get(token)
    if not session:
        return None
    if datetime.utcnow() - session["created_at"] > timedelta(hours=SESSION_TTL_HOURS):
        del _sessions[token]
        return None
    return session


def require_auth(request: Request) -> dict:
    session = get_session(request)
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


@router.post("/api/login")
async def login(body: LoginRequest, response: Response):
    if body.password != ADB_LAB_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid password")
    if not body.display_name.strip():
        raise HTTPException(status_code=400, detail="display_name is required")

    _purge_expired()
    token = secrets.token_hex(32)
    _sessions[token] = {
        "display_name": body.display_name.strip(),
        "created_at": datetime.utcnow(),
    }
    logger.info("Login: %s", body.display_name)

    response.set_cookie(
        key="adb_lab_session",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_HOURS * 3600,
    )
    return {"ok": True, "display_name": body.display_name.strip()}


@router.post("/api/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("adb_lab_session")
    if token and token in _sessions:
        del _sessions[token]
    response.delete_cookie("adb_lab_session")
    return {"ok": True}
