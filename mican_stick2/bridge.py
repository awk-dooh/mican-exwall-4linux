"""Optional SocketCAN bridge for the miCAN-Stick2.

Because the stick is not a native SocketCAN device, this bridge translates
between its DS309-3 ASCII protocol and a Linux virtual CAN interface (``vcan``),
so that standard tooling (``candump``, ``cansend``, ``python-can``) can be used
against it.

Setup (once, as root)::

    modprobe vcan
    ip link add dev vcan0 type vcan
    ip link set up vcan0

Then run::

    python -m mican_stick2.cli bridge --port /dev/ttyUSB0 --can vcan0

The bridge runs two directions concurrently:
    * stick -> vcan : received CAN frames are injected into the vcan interface.
    * vcan -> stick : frames written to vcan are transmitted on the real bus.

This module imports ``python-can`` lazily so the rest of the package works
without it installed.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from .client import CanFrame, MiCanStick2

log = logging.getLogger("mican_stick2.bridge")


class SocketCanBridge:
    """Bidirectional bridge between a miCAN-Stick2 and a SocketCAN interface."""

    def __init__(self, stick: MiCanStick2, channel: str = "vcan0") -> None:
        try:
            import can  # python-can
        except ImportError as exc:  # pragma: no cover
            raise ImportError(
                "python-can is required for the SocketCAN bridge. "
                "Install it with: pip install python-can"
            ) from exc
        self._can = can
        self._stick = stick
        self._channel = channel
        self._bus: Optional["can.BusABC"] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._bus = self._can.interface.Bus(
            channel=self._channel, interface="socketcan"
        )
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._stick_to_can, name="stick->can",
                             daemon=True),
            threading.Thread(target=self._can_to_stick, name="can->stick",
                             daemon=True),
        ]
        for t in self._threads:
            t.start()
        log.info("Bridge started between stick and %s", self._channel)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        if self._bus is not None:
            try:
                self._bus.shutdown()
            except Exception:  # noqa: BLE001
                pass
            self._bus = None
        log.info("Bridge stopped")

    def run_forever(self) -> None:
        self.start()
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    # -- directions --------------------------------------------------------
    def _stick_to_can(self) -> None:
        assert self._bus is not None
        while not self._stop.is_set():
            try:
                for frame in self._stick.receive_frames(timeout=0.5):
                    if self._stop.is_set():
                        break
                    msg = self._can.Message(
                        arbitration_id=frame.can_id,
                        data=frame.data,
                        is_extended_id=frame.extended,
                        is_remote_frame=frame.rtr,
                    )
                    self._bus.send(msg)
            except Exception as exc:  # noqa: BLE001 - keep the bridge alive
                log.warning("stick->can error: %s", exc)

    def _can_to_stick(self) -> None:
        assert self._bus is not None
        while not self._stop.is_set():
            try:
                msg = self._bus.recv(timeout=0.5)
                if msg is None:
                    continue
                frame = CanFrame(
                    can_id=msg.arbitration_id,
                    data=bytes(msg.data),
                    extended=bool(msg.is_extended_id),
                    rtr=bool(msg.is_remote_frame),
                )
                self._stick.send_frame(frame)
            except Exception as exc:  # noqa: BLE001 - keep the bridge alive
                log.warning("can->stick error: %s", exc)
