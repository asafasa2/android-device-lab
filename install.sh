#!/usr/bin/env bash
# install.sh — Android Device Lab installer
# Idempotent: safe to run multiple times.
set -euo pipefail

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${BLUE}[•]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
header(){ echo -e "\n${BOLD}$*${NC}"; }

# ── Paths ─────────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/venv"
DATA_DIR="$PROJECT_DIR/data"
SERVICE_NAME="adb-lab"
SERVICE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"
SERVICE_TMPL="$PROJECT_DIR/adb-lab.service"

# Determine the real user when running under sudo
if [[ -n "${SUDO_USER:-}" ]]; then
    RUN_USER="$SUDO_USER"
    RUN_HOME=$(getent passwd "$SUDO_USER" | cut -d: -f6)
else
    RUN_USER="$USER"
    RUN_HOME="$HOME"
fi

IS_LINUX=false; [[ "$(uname)" == "Linux" ]] && IS_LINUX=true
HAS_SYSTEMD=false
$IS_LINUX && command -v systemctl &>/dev/null && HAS_SYSTEMD=true

echo ""
echo -e "  ${BOLD}Android Device Lab — Installer${NC}"
echo    "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo    "  Project : $PROJECT_DIR"
echo    "  User    : $RUN_USER"
echo    "  OS      : $(uname -sr)"
echo ""

# ── 1. Prerequisites ──────────────────────────────────────────────────────────
header "1. Checking prerequisites"

# python3 (required)
if ! command -v python3 &>/dev/null; then
    die "python3 not found.\n   Fix: sudo apt install python3 python3-venv python3-pip"
fi
PYTHON_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)'; then
    ok "python3 $PYTHON_VER"
else
    die "Python 3.9+ required (found $PYTHON_VER)"
fi

# python3-venv (required)
if ! python3 -m venv --help &>/dev/null 2>&1; then
    die "python3-venv not available.\n   Fix: sudo apt install python3-venv"
fi

# adb (warns but continues)
if ! command -v adb &>/dev/null; then
    warn "adb not found — devices will not be detected until it is installed."
    warn "  Fix: sudo apt install adb  OR  sudo snap install android-tools"
else
    ADB_VER=$(adb version 2>&1 | head -1)
    ok "adb   — $ADB_VER"
fi

# ffmpeg (warns but continues)
if ! command -v ffmpeg &>/dev/null; then
    warn "ffmpeg not found — screen mirroring will be unavailable."
    warn "  Fix: sudo apt install ffmpeg"
else
    FFMPEG_VER=$(ffmpeg -version 2>&1 | awk 'NR==1{print $1" "$2" "$3}')
    ok "ffmpeg — $FFMPEG_VER"
fi

# curl (needed for health-check in README examples)
command -v curl &>/dev/null && ok "curl" || warn "curl not found (optional)"

# ── 2. Directories ────────────────────────────────────────────────────────────
header "2. Creating directories"

mkdir -p "$DATA_DIR"
ok "data/  — $DATA_DIR"

# ── 3. Virtual environment ────────────────────────────────────────────────────
header "3. Python virtual environment"

if [[ -d "$VENV_DIR" && -x "$VENV_DIR/bin/python" ]]; then
    info "venv exists, upgrading packages…"
else
    info "Creating venv at $VENV_DIR…"
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$PROJECT_DIR/requirements.txt"
ok "Dependencies installed ($(wc -l < "$PROJECT_DIR/requirements.txt" | tr -d ' ') packages)"

# ── 4. Systemd service ────────────────────────────────────────────────────────
header "4. Systemd service"

if ! $HAS_SYSTEMD; then
    warn "Systemd not available ($(uname)) — skipping service installation."
    echo ""
    echo "  To start the server manually:"
    echo "    cd $PROJECT_DIR"
    echo "    $VENV_DIR/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000"
    echo ""
elif [[ "$EUID" -ne 0 ]]; then
    warn "Not running as root — skipping service installation."
    warn "Re-run with sudo to install the systemd service:"
    warn "  sudo bash $PROJECT_DIR/install.sh"
else
    if [[ ! -f "$SERVICE_TMPL" ]]; then
        die "Service template not found: $SERVICE_TMPL"
    fi

    info "Writing $SERVICE_DEST…"
    sed \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__RUN_USER__|$RUN_USER|g" \
        "$SERVICE_TMPL" > "$SERVICE_DEST"
    chmod 644 "$SERVICE_DEST"

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" --quiet
    systemctl restart "$SERVICE_NAME"

    # Wait up to 8 s for the service to become active
    for i in $(seq 1 8); do
        sleep 1
        systemctl is-active --quiet "$SERVICE_NAME" && break
    done

    if systemctl is-active --quiet "$SERVICE_NAME"; then
        ok "Service '${SERVICE_NAME}' is active"
    else
        warn "Service did not start cleanly."
        warn "  Investigate: sudo journalctl -u $SERVICE_NAME -n 40 --no-pager"
    fi
fi

# ── 5. Detect server IP ───────────────────────────────────────────────────────
SERVER_IP=$(python3 - <<'EOF'
import socket
try:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("8.8.8.8", 80))
        print(s.getsockname()[0])
except Exception:
    print("127.0.0.1")
EOF
)

# ── 6. Summary ────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  Android Device Lab — Installation Complete${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Web UI    →  http://${SERVER_IP}:8000"
echo "  ADB remote→  adb -H ${SERVER_IP} -P 5037 devices"
echo ""
echo "  Default credentials:"
echo "    Password     :  adblab123       (ADB_LAB_PASSWORD)"
echo "    CI API key   :  ci-key-change-me (ADB_LAB_CI_API_KEY)"
echo ""
echo -e "  ${YELLOW}Change these before exposing the server to your network!${NC}"
if $HAS_SYSTEMD && [[ "$EUID" -eq 0 ]]; then
    echo "  Edit: $SERVICE_DEST"
    echo "  Apply: sudo systemctl daemon-reload && sudo systemctl restart $SERVICE_NAME"
fi
echo ""
echo "  Logs:  sudo journalctl -u $SERVICE_NAME -f"
echo "  Stop:  sudo systemctl stop $SERVICE_NAME"
echo ""
