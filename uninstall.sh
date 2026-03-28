#!/usr/bin/env bash
# uninstall.sh — Android Device Lab uninstaller
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${BLUE}[•]${NC} $*"; }
ok()    { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="adb-lab"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

echo ""
echo -e "  ${BOLD}Android Device Lab — Uninstaller${NC}"
echo    "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo    "  Project: $PROJECT_DIR"
echo ""

# ── 1. Stop and disable systemd service ──────────────────────────────────────
if command -v systemctl &>/dev/null && [[ -f "$SERVICE_FILE" ]]; then
    if [[ "$EUID" -ne 0 ]]; then
        die "Root required to remove the systemd service. Re-run with sudo."
    fi

    info "Stopping service '${SERVICE_NAME}'…"
    systemctl stop "$SERVICE_NAME"   2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    ok "Service stopped and disabled"

    info "Removing $SERVICE_FILE…"
    rm -f "$SERVICE_FILE"
    systemctl daemon-reload
    ok "Service file removed"
else
    warn "No systemd service found (nothing to stop)"
fi

# ── 2. Remove virtual environment ─────────────────────────────────────────────
VENV_DIR="$PROJECT_DIR/venv"
if [[ -d "$VENV_DIR" ]]; then
    info "Removing virtual environment…"
    rm -rf "$VENV_DIR"
    ok "venv/ removed"
fi

# ── 3. Optionally remove project directory ────────────────────────────────────
echo ""
echo -e "${YELLOW}The project directory will NOT be removed by default.${NC}"
echo    "  $PROJECT_DIR"
echo    "  (Contains your data/, configuration, and source code.)"
echo ""
read -rp "  Remove the entire project directory? [y/N] " CONFIRM
CONFIRM="${CONFIRM,,}"

if [[ "$CONFIRM" == "y" || "$CONFIRM" == "yes" ]]; then
    # Safety: refuse to rm / or $HOME
    if [[ "$PROJECT_DIR" == "/" || "$PROJECT_DIR" == "$HOME" ]]; then
        die "Refusing to remove root or home directory."
    fi
    info "Removing $PROJECT_DIR…"
    rm -rf "$PROJECT_DIR"
    ok "Project directory removed"
else
    ok "Project directory kept: $PROJECT_DIR"
    echo "  To remove later: rm -rf $PROJECT_DIR"
fi

echo ""
ok "Uninstall complete."
echo ""
