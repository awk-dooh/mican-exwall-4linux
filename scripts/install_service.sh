#!/usr/bin/env bash
#
# Install the Exwall totem supervisor as a systemd service.
#
# It generates /etc/systemd/system/mican-totem.service from the template in
# systemd/mican-totem.service.in, filling in the real paths and user, then
# enables and starts it so keep_totem_happy.py runs on boot.
#
# Usage:
#   sudo ./scripts/install_service.sh            # install + enable + start
#   sudo ./scripts/install_service.sh --port /dev/ttyUSB0
#   sudo ./scripts/install_service.sh --uninstall
#
# Re-run any time; it is safe to run repeatedly.

set -euo pipefail

say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mXX \033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Linux" ] || die "This installer is for Linux only."
command -v systemctl >/dev/null 2>&1 || die "systemd (systemctl) not found."

SERVICE_NAME="mican-totem.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="/dev/mican0"

# ---- parse args ----------------------------------------------------------
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) die "Unknown option: $1" ;;
    esac
done

# ---- need root to write into /etc/systemd --------------------------------
if [ "$(id -u)" -ne 0 ]; then
    die "Please run with sudo: sudo ./scripts/install_service.sh"
fi

# ---- uninstall path ------------------------------------------------------
if [ "$UNINSTALL" -eq 1 ]; then
    say "Stopping and disabling ${SERVICE_NAME}"
    systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
    rm -f "${UNIT_PATH}"
    systemctl daemon-reload
    say "Removed ${SERVICE_NAME}."
    exit 0
fi

# ---- resolve the install user (the human, not root) ----------------------
RUN_USER="${SUDO_USER:-root}"
[ "$RUN_USER" != "root" ] || warn "Installing to run as 'root' (no SUDO_USER detected)."

# ---- locate the venv python ---------------------------------------------
VENV="${SCRIPT_DIR}/.venv"
if [ ! -x "${VENV}/bin/python" ]; then
    die "Virtualenv not found at ${VENV}. Run ./scripts/install.sh first."
fi

TEMPLATE="${SCRIPT_DIR}/systemd/mican-totem.service.in"
[ -f "$TEMPLATE" ] || die "Template not found: $TEMPLATE"

say "Generating ${UNIT_PATH}"
say "  user      = ${RUN_USER}"
say "  workdir   = ${SCRIPT_DIR}"
say "  venv      = ${VENV}"
say "  port      = ${PORT}"

sed -e "s|@USER@|${RUN_USER}|g" \
    -e "s|@WORKDIR@|${SCRIPT_DIR}|g" \
    -e "s|@VENV@|${VENV}|g" \
    -e "s|@PORT@|${PORT}|g" \
    "$TEMPLATE" > "$UNIT_PATH"

say "Reloading systemd and enabling the service"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo
say "Done. Useful commands:"
echo "  systemctl status  ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f      # live logs"
echo "  sudo systemctl restart ${SERVICE_NAME}"
echo "  sudo ./scripts/install_service.sh --uninstall"
