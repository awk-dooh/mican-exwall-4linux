#!/usr/bin/env bash
#
# Create and bring up a virtual SocketCAN interface (vcan0) so that the
# miCAN-Stick2 SocketCAN bridge can expose received frames to candump/cansend
# and python-can.
#
# Usage:   sudo ./scripts/setup_vcan.sh [interface_name]
#          (default interface name is vcan0)

set -euo pipefail

IFACE="${1:-vcan0}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Please run as root:  sudo $0 $IFACE" >&2
    exit 1
fi

echo "==> Loading vcan kernel module"
modprobe vcan

if ip link show "$IFACE" >/dev/null 2>&1; then
    echo "==> $IFACE already exists"
else
    echo "==> Creating $IFACE"
    ip link add dev "$IFACE" type vcan
fi

echo "==> Bringing $IFACE up"
ip link set up "$IFACE"

echo "==> Done. Verify with:  ip -details link show $IFACE"
echo "    Then in one terminal:  candump $IFACE"
echo "    And run the bridge:    mican bridge --can $IFACE --port /dev/mican0"
