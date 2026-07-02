"""
Robust Linux driver/client for the miControl miCAN-Stick2 USB-CAN gateway.

The miCAN-Stick2 exposes a USB **virtual serial port** and speaks an ASCII
protocol based on CiA 309-3 (DS309-3). It is therefore driven from user space
over the serial port; no proprietary kernel module is required (the in-kernel
``ftdi_sio`` / ``cdc_acm`` drivers enumerate the device automatically).

Public API::

    from mican_stick2 import MiCanStick2, find_stick_port

    with MiCanStick2(port="/dev/ttyUSB0") as stick:
        print(stick.version())
        stick.set_bitrate(500_000)
        stick.start()
        for frame in stick.receive_frames(timeout=5):
            print(frame)
"""
from .transport import SerialTransport, TransportError
from .client import MiCanStick2, CanFrame, ProtocolError, StickError
from .discovery import find_stick_port, list_candidate_ports

__all__ = [
    "MiCanStick2",
    "CanFrame",
    "SerialTransport",
    "TransportError",
    "ProtocolError",
    "StickError",
    "find_stick_port",
    "list_candidate_ports",
]

__version__ = "1.0.0"
