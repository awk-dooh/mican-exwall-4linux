"""Command-line interface for the miCAN-Stick2 driver.

Examples::

    # List candidate serial ports and how well they match
    python -m mican_stick2.cli detect

    # Query firmware version (auto-detect port)
    python -m mican_stick2.cli version

    # Configure bitrate, go operational, and dump received frames
    python -m mican_stick2.cli monitor --port /dev/ttyUSB0 --bitrate 500000

    # Send a single frame
    python -m mican_stick2.cli send --id 0x123 --data 00112233 --port /dev/ttyUSB0

    # Bridge the stick to a virtual SocketCAN interface
    python -m mican_stick2.cli bridge --can vcan0 --port /dev/ttyUSB0
"""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from .client import CanFrame, MiCanStick2
from .discovery import find_stick_port, list_candidate_ports
from .transport import SerialConfig


def _resolve_port(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    port = find_stick_port(required=True)
    print(f"Auto-detected port: {port}", file=sys.stderr)
    return port


def _make_stick(args: argparse.Namespace) -> MiCanStick2:
    port = _resolve_port(args.port)
    cfg = SerialConfig(port=port, baudrate=args.baudrate)
    return MiCanStick2(
        port=port,
        serial_config=cfg,
        net=args.net,
        node=args.node,
        command_timeout=args.timeout,
    )


def cmd_detect(args: argparse.Namespace) -> int:
    ports = list_candidate_ports()
    if not ports:
        print("No serial ports found.")
        return 1
    print(f"{'DEVICE':<20} {'VID:PID':<12} DESCRIPTION")
    for p in ports:
        print(f"{p.device:<20} {p.vidpid or '?':<12} {p.description}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    with _make_stick(args) as stick:
        print(stick.version())
    return 0


def cmd_monitor(args: argparse.Namespace) -> int:
    with _make_stick(args) as stick:
        if args.bitrate:
            stick.set_bitrate(args.bitrate)
        stick.start()
        print("Listening (Ctrl-C to stop)...", file=sys.stderr)
        try:
            for frame in stick.receive_frames(timeout=None):
                print(frame)
        except KeyboardInterrupt:
            pass
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    data = bytes.fromhex(args.data) if args.data else b""
    frame = CanFrame(
        can_id=int(args.id, 0),
        data=data,
        extended=args.extended,
        rtr=args.rtr,
    )
    with _make_stick(args) as stick:
        if args.bitrate:
            stick.set_bitrate(args.bitrate)
        stick.start()
        stick.send_frame(frame)
        print(f"Sent: {frame}")
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    from .bridge import SocketCanBridge  # lazy: needs python-can

    with _make_stick(args) as stick:
        if args.bitrate:
            stick.set_bitrate(args.bitrate)
        stick.start()
        bridge = SocketCanBridge(stick, channel=args.can)
        print(f"Bridging stick <-> {args.can} (Ctrl-C to stop)...",
              file=sys.stderr)
        bridge.run_forever()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mican_stick2",
        description="Robust Linux driver/CLI for the miControl miCAN-Stick2.",
    )
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Increase log verbosity (repeatable).")

    # Shared connection options.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", help="Serial device (default: auto-detect).")
    common.add_argument("--baudrate", type=int, default=115200,
                        help="Serial baud rate (default: 115200).")
    common.add_argument("--net", type=int, default=None,
                        help="DS309-3 network id (default: omitted; it is "
                             "ignored by the device).")
    common.add_argument("--node", type=int, default=0,
                        help="DS309-3 node id, 0 = broadcast (default: 0).")
    common.add_argument("--timeout", type=float, default=1.5,
                        help="Per-command timeout in seconds (default: 1.5).")
    common.add_argument("--bitrate", type=int, default=None,
                        help="CAN bitrate in bit/s to configure before use.")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("detect", help="List candidate serial ports.").set_defaults(
        func=cmd_detect)

    sub.add_parser("version", parents=[common],
                   help="Query firmware version.").set_defaults(func=cmd_version)

    sub.add_parser("monitor", parents=[common],
                   help="Dump received CAN frames.").set_defaults(
        func=cmd_monitor)

    sp_send = sub.add_parser("send", parents=[common], help="Send one CAN frame.")
    sp_send.add_argument("--id", required=True,
                         help="CAN identifier (e.g. 0x123).")
    sp_send.add_argument("--data", default="",
                         help="Payload as hex (e.g. 00112233).")
    sp_send.add_argument("--extended", action="store_true",
                         help="Use a 29-bit extended identifier.")
    sp_send.add_argument("--rtr", action="store_true",
                         help="Send a remote transmission request.")
    sp_send.set_defaults(func=cmd_send)

    sp_bridge = sub.add_parser("bridge", parents=[common],
                               help="Bridge to a SocketCAN interface.")
    sp_bridge.add_argument("--can", default="vcan0",
                           help="SocketCAN channel (default: vcan0).")
    sp_bridge.set_defaults(func=cmd_bridge)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    level = logging.WARNING - min(args.verbose, 2) * 10
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level friendly error
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
