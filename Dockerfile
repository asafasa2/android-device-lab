# ─────────────────────────────────────────────────────────────────────────────
# Android Device Lab — Docker image
#
# Build:  docker build -t android-device-lab .
# Run:    docker compose up -d        (see docker-compose.yml)
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.11-slim

LABEL org.opencontainers.image.title="Android Device Lab"
LABEL org.opencontainers.image.description="Shared ADB server and web UI for Android device management"

# ── System dependencies ───────────────────────────────────────────────────────
# android-tools-adb : adb binary that talks to USB devices
# ffmpeg            : used by screen mirroring (mirror.py)
# curl              : health-check in compose
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        android-tools-adb \
        ffmpeg \
        curl \
 && rm -rf /var/lib/apt/lists/*

# ── Python dependencies (separate layer for cache efficiency) ─────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# ── Application files ─────────────────────────────────────────────────────────
COPY backend/   ./backend/
COPY frontend/  ./frontend/

# Persistent data directory (SQLite database lives here)
RUN mkdir -p /app/data

# ── Runtime configuration ─────────────────────────────────────────────────────
# All values can be overridden at runtime via environment variables
# (e.g. in docker-compose.yml or `docker run -e`).
ENV ADB_LAB_PASSWORD=adblab123 \
    ADB_LAB_PORT=8000 \
    ADB_PATH=adb \
    RESERVATION_TIMEOUT_HOURS=2 \
    DEVICE_POLL_INTERVAL=5 \
    ADB_LAB_CI_API_KEY=ci-key-change-me
# ADB_LAB_SERVER_IP is auto-detected from the container's network interface

# ── Ports ─────────────────────────────────────────────────────────────────────
# 8000 — Web UI + REST API + WebSocket
# 5037 — ADB server (for remote `adb -H <host> -P 5037` access)
EXPOSE 8000
EXPOSE 5037

# ── Health check ──────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8000/api/status || exit 1

# ── Entrypoint ────────────────────────────────────────────────────────────────
# The app calls `adb -a -P 5037 start-server` during startup (devices.py),
# binding the ADB server to 0.0.0.0:5037 so containers and remote hosts
# can connect with `adb -H <container-ip> -P 5037`.
CMD ["uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--log-level", "info"]
