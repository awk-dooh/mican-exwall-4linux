#!/usr/bin/env python3
"""keep_totem_happy.py — a small supervisor for the Exwall totem.

Runs on the NUC (the machine wired to the miCAN-Stick2). It:

  * keeps the Edge-Backlight relay ON while the surge-protector input is OK,
  * runs the cooling fan, speeding it up as the temperature sensor voltage rises.

Wiring (from Schaltplan DT.0618_Rev.03):
  * mcDSA-E60  = Node 1 : fan drive; NTC temperature sensor on analog input 0.
  * mcIO-K1    = Node 4 : Edge-Backlight relay on Dout3; overvoltage status on Din0.

Usage:
    python3 keep_totem_happy.py [--port /dev/mican0] [--dry-run]

This is example/reference code. Verify the pin/bit mapping for your unit and
replace ``mv_to_celsius`` with the real NTC curve before relying on it.
"""
from __future__ import annotations

import argparse
import time

from mican_stick2 import MiCanStick2, find_stick_port

FAN_NODE = 1
IO_NODE = 4

BACKLIGHT_MASK = 0x08   # Dout3
OVERVOLTAGE_BIT = 0x01  # Din0

FAN_SPEED_MIN = 10_000
FAN_SPEED_MAX = 40_000


def mv_to_celsius(mv: int) -> float:
    """Placeholder NTC conversion — replace with the fitted sensor's curve."""
    return (mv - 500) / 20.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", default=None,
                    help="Serial device (default: auto-detect).")
    ap.add_argument("--bitrate", type=int, default=1_000_000,
                    help="CAN bitrate in bit/s (default: 1000000).")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="Loop period in seconds (default: 2.0).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Read/report only; do not switch outputs or move the fan.")
    args = ap.parse_args()

    port = args.port or find_stick_port(required=True)

    with MiCanStick2(port=port) as stick:
        stick.set_bitrate(args.bitrate)
        stick.start(0)  # all nodes operational

        if not args.dry_run:
            stick.reset_fault(node=FAN_NODE)
            stick.enable_drive(node=FAN_NODE, mode=3)  # profile velocity

        backlight_on = False
        print("Supervising totem (Ctrl-C to stop)...")
        while True:
            inputs = stick.read_inputs(node=IO_NODE)
            power_ok = bool(inputs & OVERVOLTAGE_BIT)

            if power_ok and not backlight_on:
                if not args.dry_run:
                    stick.set_outputs(BACKLIGHT_MASK, node=IO_NODE)
                backlight_on = True
                print("Backlight ON")
            elif not power_ok and backlight_on:
                if not args.dry_run:
                    stick.set_outputs(0x00, node=IO_NODE)
                backlight_on = False
                print("Power problem -> backlight OFF")

            mv = stick.read_analog_input(0, node=FAN_NODE)
            temp = mv_to_celsius(mv)
            speed = max(FAN_SPEED_MIN, min(FAN_SPEED_MAX, mv * 10))
            if not args.dry_run:
                stick.set_velocity(speed, node=FAN_NODE)

            print(f"power_ok={power_ok}  sensor={mv} mV (~{temp:.1f} C)  "
                  f"fan_speed={speed}")
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
