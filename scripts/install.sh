#!/usr/bin/env bash
#
# Monkey-proof installer for the miCAN-Stick2 Linux driver.
#
# It:
#   1. checks you are on Linux with Python 3,
#   2. creates a virtual environment in ./.venv,
#   3. installs the driver and its dependencies,
#   4. adds you to the 'dialout' group (needed to open the serial port),
#   5. installs a udev rule so the stick always appears as /dev/mican0,
#   6. runs a quick self-test.
#
# Usage:   ./scripts/install.sh
# Re-run it any time; it is safe to run repeatedly.

set -euo pipefail

# ---- pretty output -------------------------------------------------------
say()  { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mXX \033[0m %s\n' "$*" >&2; exit 1; }

# ---- 0. sanity checks ----------------------------------------------------
[ "$(uname -s)" = "Linux" ] || die "This installer is for Linux only."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"
say "Project directory: $SCRIPT_DIR"

command -v python3 >/dev/null 2>&1 || die "python3 is not installed. Install it first (e.g. 'sudo apt install python3 python3-venv')."

PYV="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Found Python $PYV"

# ---- 1. virtual environment ---------------------------------------------
if [ ! -d ".venv" ]; then
    say "Creating virtual environment in ./.venv"
    python3 -m venv .venv || die "Could not create venv. Try 'sudo apt install python3-venv'."
else
    say "Virtual environment already exists, reusing it."
fi

# shellcheck disable=SC1091
source .venv/bin/activate

# ---- 2. install the driver ----------------------------------------------
say "Upgrading pip"
python -m pip install --quiet --upgrade pip

say "Installing the driver and dependencies"
python -m pip install --quiet -e ".[bridge]" || python -m pip install --quiet -e .

# ---- 3. serial port permissions -----------------------------------------
if getent group dialout >/dev/null 2>&1; then
    if id -nG "$USER" | tr ' ' '\n' | grep -qx dialout; then
        say "User '$USER' is already in the 'dialout' group."
    else
        say "Adding user '$USER' to the 'dialout' group (needs sudo)."
        sudo usermod -aG dialout "$USER" && \
            warn "Log out and back in (or reboot) for group membership to take effect."
    fi
fi

# ---- 4. udev rule: stable /dev/mican0 name ------------------------------
UDEV_RULE="/etc/udev/rules.d/99-mican-stick2.rules"
if [ -w /etc/udev/rules.d ] || sudo -n true 2>/dev/null || true; then
    say "Installing udev rule for a stable /dev/mican0 symlink (needs sudo)."
    # FTDI-based VCP is the common case; adjust idVendor/idProduct after you
    # confirm them with 'lsusb'. The MODE line also grants access without sudo.
    sudo tee "$UDEV_RULE" >/dev/null <<'EOF'
# miControl miCAN-Stick2 USB-CAN gateway (virtual COM port).
# Confirm your device's IDs with `lsusb`; the default matches FTDI FT232/FT231X.
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", SYMLINK+="mican0", MODE="0660", GROUP="dialout"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6015", SYMLINK+="mican0", MODE="0660", GROUP="dialout"
EOF
    sudo udevadm control --reload-rules && sudo udevadm trigger || \
        warn "Could not reload udev rules automatically; replug the stick."
else
    warn "Skipped udev rule (no sudo). You can still use /dev/ttyUSB0 directly."
fi

# ---- 5. self-test --------------------------------------------------------
say "Running offline self-test"
if python -m pip show pytest >/dev/null 2>&1 || python -m pip install --quiet pytest; then
    python -m pytest -q tests/ || warn "Self-test reported problems (see above)."
fi

say "Done!"
echo
echo "Next steps:"
echo "  1. Plug in the miCAN-Stick2."
echo "  2. source .venv/bin/activate"
echo "  3. mican detect          # find the port"
echo "  4. mican version         # talk to the stick"
echo
echo "If you were just added to 'dialout', log out and back in first."
