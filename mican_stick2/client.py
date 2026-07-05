"""DS309-3 (CiA 309-3) ASCII client for the miCAN-Stick2.

Protocol confirmed against the miControl manual (AN1301 "miControl
Text-Kommandos") and the miCAN-Stick2 technical data.

Wire format::

    request:       ["seq"] [[net] node] command <args...>   (CRLF terminated)
    response:      ["seq"] OK
                   ["seq"] Error <code>
                   ["seq"] <data>                            (read / info)
    notification:  [net] MSG <cob_id> <len> 0xXX ...         (unsolicited)

Received CAN frames arrive asynchronously as ``MSG`` notification lines and may
be interleaved with command responses (e.g. ``start`` returns ``OK`` but also
triggers boot-up ``MSG`` lines). This client buffers those notifications so
they are never lost while a command's response is awaited.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterator, List, Optional

from .transport import SerialConfig, SerialTransport, TransportError

log = logging.getLogger("mican_stick2.client")

# Extended (29-bit) identifier flag as used by the firmware in cob_id.
EXTENDED_FLAG = 0x20000000
CAN_EXT_MASK = 0x1FFFFFFF
CAN_STD_MASK = 0x7FF

# The ``init <baud>`` argument is expressed in kBit/s.
_BITRATE_KBIT = (1000, 800, 500, 250, 125, 100, 50, 20)
SUPPORTED_BITRATES = tuple(k * 1000 for k in _BITRATE_KBIT)

# --- CANopen profile object dictionary indices ---------------------------
# DS402 (drive & motion profile, e.g. mcDSA-E60 servo)
OD_CONTROLWORD = 0x6040
OD_STATUSWORD = 0x6041
OD_MODES_OF_OPERATION = 0x6060
OD_MODES_OF_OPERATION_DISPLAY = 0x6061
OD_POSITION_ACTUAL = 0x6064
OD_VELOCITY_ACTUAL = 0x606C
OD_TARGET_POSITION = 0x607A
OD_PROFILE_VELOCITY = 0x6081
OD_PROFILE_ACCELERATION = 0x6083
OD_TARGET_TORQUE = 0x6071
OD_TARGET_VELOCITY = 0x60FF
# DS401 (generic I/O profile, e.g. mcIO-K1 I/O module)
OD_DIGITAL_INPUTS = 0x6000
OD_DIGITAL_OUTPUTS = 0x6200
# DS301 identity object
OD_DEVICE_TYPE = 0x1000
OD_IDENTITY = 0x1018

# DS402 controlword command bit patterns (drive state machine)
CW_DISABLE_VOLTAGE = 0x0000
CW_SHUTDOWN = 0x0006
CW_SWITCH_ON = 0x0007
CW_ENABLE_OPERATION = 0x000F
CW_FAULT_RESET = 0x0080
CW_NEW_SETPOINT = 0x001F   # enable operation + set-point bit (bit 4)

# DS402 modes of operation (object 0x6060)
MODE_PROFILE_POSITION = 1
MODE_PROFILE_VELOCITY = 3
MODE_PROFILE_TORQUE = 4
MODE_HOMING = 6

# DS402 statusword (0x6041) bit masks
SW_READY_TO_SWITCH_ON = 1 << 0
SW_SWITCHED_ON = 1 << 1
SW_OPERATION_ENABLED = 1 << 2
SW_FAULT = 1 << 3
SW_TARGET_REACHED = 1 << 10


class ProtocolError(Exception):
    """The device returned a malformed or unexpected response."""


class StickError(Exception):
    """The device returned an explicit ``Error <code>`` response."""

    #: human-readable descriptions for the documented error codes.
    DESCRIPTIONS = {
        100: "command not supported",
        101: "syntax error (command could not be interpreted)",
        -541: "SdoWriteError (object could not be written)",
        -542: "SdoReadError (object could not be read)",
        -571: "BadCommand (value out of range)",
        -582: "TxTimeout (baudrate/terminator/bus wiring)",
        -583: "ResponseTimeout (device off or not answering)",
    }

    def __init__(self, code: int, request: str) -> None:
        desc = self.DESCRIPTIONS.get(code, "unknown error")
        super().__init__(f"Device error {code} ({desc}) for request: {request!r}")
        self.code = code
        self.request = request


@dataclass(frozen=True)
class CanFrame:
    """A single CAN 2.0B frame."""

    can_id: int
    data: bytes = b""
    extended: bool = False   # 29-bit identifier
    rtr: bool = False        # remote transmission request

    def __post_init__(self) -> None:
        if not self.extended and self.can_id > CAN_STD_MASK:
            raise ValueError("11-bit CAN id out of range; set extended=True")
        if self.extended and self.can_id > CAN_EXT_MASK:
            raise ValueError("29-bit CAN id out of range")
        if len(self.data) > 8:
            raise ValueError("CAN 2.0 data length must be 0..8 bytes")

    def __str__(self) -> str:
        kind = "EXT" if self.extended else "STD"
        flag = " RTR" if self.rtr else ""
        return (f"{kind}{flag} 0x{self.can_id:X} [{len(self.data)}] "
                f"{self.data.hex(' ').upper()}")


class MiCanStick2:
    """High-level, robust client for the miCAN-Stick2."""

    def __init__(
        self,
        port: str,
        *,
        serial_config: Optional[SerialConfig] = None,
        net: Optional[int] = None,
        node: int = 0,
        command_timeout: float = 1.5,
        max_command_retries: int = 2,
        use_sequence_numbers: bool = True,
        max_reconnect_attempts: int = 0,
    ) -> None:
        cfg = serial_config or SerialConfig(port=port)
        cfg.port = port
        self._transport = SerialTransport(
            cfg, max_reconnect_attempts=max_reconnect_attempts
        )
        self._net = net            # None => omit net field (it is ignored anyway)
        self._node = node
        self._timeout = command_timeout
        self._retries = max_command_retries
        self._use_seq = use_sequence_numbers
        self._seq = 0
        self._lock = threading.RLock()
        self._rx_frames: Deque[CanFrame] = deque(maxlen=10000)

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        self._transport.open()

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> "MiCanStick2":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- core request/response --------------------------------------------
    def _next_seq(self) -> int:
        self._seq = (self._seq % 0xFFFFFFFF) + 1
        return self._seq

    def _command(
        self,
        body: str,
        *,
        timeout: Optional[float] = None,
        expect_response: bool = True,
    ) -> str:
        """Send a fully-formed command body and return the response payload.

        ``body`` must already include any ``net``/``node`` prefix required by
        the specific command (the addressing rules differ per command in
        DS309-3, so each helper composes it via :meth:`_addr`).
        """
        timeout = self._timeout if timeout is None else timeout
        last_exc: Optional[Exception] = None
        for attempt in range(self._retries + 1):
            seq = self._next_seq() if self._use_seq else None
            line = f"[{seq}] {body}" if seq is not None else body
            with self._lock:
                try:
                    self._transport.write_line(line)
                    if not expect_response:
                        return ""
                    return self._read_response(seq, timeout, request=line)
                except (TransportError, ProtocolError) as exc:
                    last_exc = exc
                    log.warning("Command %r failed (attempt %d/%d): %s",
                                line, attempt + 1, self._retries + 1, exc)
                    continue
        assert last_exc is not None
        raise last_exc

    def _read_response(
        self, seq: Optional[int], timeout: float, request: str
    ) -> str:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError(f"Timeout waiting for reply to {request!r}")
            raw = self._transport.read_line(timeout=remaining)
            if raw is None:
                raise TransportError(f"Timeout waiting for reply to {request!r}")
            line = raw.strip()
            if not line:
                continue

            # Buffer asynchronous MSG notifications that arrive before/among the
            # command response instead of mistaking them for the reply.
            frame = self._parse_frame(line)
            if frame is not None:
                self._rx_frames.append(frame)
                continue

            resp_seq, payload = self._split_sequence(line)
            if seq is not None and resp_seq is not None and resp_seq != seq:
                log.debug("Ignoring out-of-sequence reply: %r", line)
                continue

            m = re.match(r"(?i)^Error\b[:=]?\s*(-?\d+)?", payload)
            if m:
                code = int(m.group(1)) if m.group(1) else 0
                raise StickError(code, request)
            return payload

    # -- addressing helpers ------------------------------------------------
    def _addr(self, node: Optional[int], net: Optional[int]) -> str:
        """Build the optional ``[net] node`` prefix for a command."""
        net = self._net if net is None else net
        parts: List[str] = []
        if node is not None:
            if net is not None:
                parts.append(str(net))
            parts.append(str(node))
        return " ".join(parts)

    @staticmethod
    def _split_sequence(line: str) -> tuple[Optional[int], str]:
        m = re.match(r"^\[(\d+)\]\s*(.*)$", line)
        if m:
            return int(m.group(1)), m.group(2).strip()
        return None, line

    # -- high-level operations --------------------------------------------
    def version(self) -> str:
        """``info version`` -> raw version string (no net/node prefix)."""
        return self._command("info version")

    def version_info(self) -> dict:
        """Parsed ``info version`` fields."""
        parts = self.version().split()
        keys = ("vendor_id", "product_code", "firmware_version",
                "serial_number", "network_class", "protocol_version",
                "implementation_class")
        return dict(zip(keys, parts))

    def set_bitrate(self, bitrate: int) -> None:
        """``[net] init <kBit/s>`` — set CAN bitrate (bit/s) and activate bus."""
        if bitrate not in SUPPORTED_BITRATES:
            raise ValueError(
                f"Unsupported bitrate {bitrate} bit/s; choose from "
                f"{SUPPORTED_BITRATES}"
            )
        kbit = bitrate // 1000
        prefix = "" if self._net is None else f"{self._net} "
        self._command(f"{prefix}init {kbit}")

    def can_off(self) -> None:
        """Deactivate the CAN bus (``init -1``)."""
        prefix = "" if self._net is None else f"{self._net} "
        self._command(f"{prefix}init -1")

    def start(self, node: Optional[int] = None, net: Optional[int] = None) -> None:
        """NMT start (Operational). node=0 => broadcast."""
        node = self._node if node is None else node
        self._command(f"{self._addr(node, net)} start".strip())

    def stop(self, node: Optional[int] = None, net: Optional[int] = None) -> None:
        """NMT stop. node=0 => broadcast."""
        node = self._node if node is None else node
        self._command(f"{self._addr(node, net)} stop".strip())

    def preop(self, node: Optional[int] = None, net: Optional[int] = None) -> None:
        """NMT pre-operational. node=0 => broadcast."""
        node = self._node if node is None else node
        self._command(f"{self._addr(node, net)} preop".strip())

    def reset_node(self, node: Optional[int] = None,
                   net: Optional[int] = None) -> None:
        node = self._node if node is None else node
        self._command(f"{self._addr(node, net)} reset node".strip())

    def reset_comm(self, node: Optional[int] = None,
                   net: Optional[int] = None) -> None:
        node = self._node if node is None else node
        self._command(f"{self._addr(node, net)} reset comm".strip())

    def set_sdo_timeout(self, milliseconds: int) -> None:
        prefix = "" if self._net is None else f"{self._net} "
        self._command(f"{prefix}set sdo_timeout {int(milliseconds)}")

    # -- CANopen SDO -------------------------------------------------------
    def read(self, index: int, subindex: int, datatype: str = "i32",
             node: Optional[int] = None, net: Optional[int] = None) -> str:
        """``[[net] node] r <index> <subindex> <datatype>`` (SDO read)."""
        node = self._node if node is None else node
        body = f"{self._addr(node, net)} r 0x{index:X} {subindex} {datatype}"
        return self._command(body.strip())

    def write(self, index: int, subindex: int, datatype: str, value: int,
              node: Optional[int] = None, net: Optional[int] = None) -> None:
        """``[[net] node] w <index> <subindex> <datatype> <value>`` (SDO write)."""
        node = self._node if node is None else node
        body = (f"{self._addr(node, net)} w 0x{index:X} {subindex} "
                f"{datatype} {value}")
        self._command(body.strip())

    def read_int(self, index: int, subindex: int, datatype: str = "i32",
                 node: Optional[int] = None, net: Optional[int] = None) -> int:
        """Like :meth:`read`, but parse the reply into an ``int``.

        The device returns the value as text, decimal or ``0x``-prefixed hex.
        """
        payload = self.read(index, subindex, datatype, node=node, net=net)
        tokens = payload.split()
        if not tokens:
            raise ProtocolError(f"Empty value in read reply: {payload!r}")
        token = tokens[-1]
        try:
            return int(token, 16) if token.lower().startswith("0x") \
                else int(token, 10)
        except ValueError as exc:
            raise ProtocolError(
                f"Could not parse integer from reply {payload!r}") from exc

    # -- CANopen DS402 drive helpers (e.g. mcDSA-E60) ----------------------
    def reset_fault(self, node: Optional[int] = None,
                    net: Optional[int] = None) -> None:
        """Clear a drive fault (controlword fault-reset edge)."""
        self.write(OD_CONTROLWORD, 0, "u16", CW_FAULT_RESET, node=node, net=net)

    def set_mode(self, mode: int, node: Optional[int] = None,
                 net: Optional[int] = None) -> None:
        """Set DS402 modes of operation (object 0x6060)."""
        self.write(OD_MODES_OF_OPERATION, 0, "i8", mode, node=node, net=net)

    def enable_drive(self, node: Optional[int] = None, *,
                     mode: Optional[int] = MODE_PROFILE_VELOCITY,
                     net: Optional[int] = None) -> None:
        """Walk the DS402 state machine to *Operation enabled*.

        Sequence: Shutdown (0x06) -> Switch on (0x07) -> [set mode] ->
        Enable operation (0x0F). Pass ``mode=None`` to leave the current mode
        untouched.
        """
        self.write(OD_CONTROLWORD, 0, "u16", CW_SHUTDOWN, node=node, net=net)
        self.write(OD_CONTROLWORD, 0, "u16", CW_SWITCH_ON, node=node, net=net)
        if mode is not None:
            self.set_mode(mode, node=node, net=net)
        self.write(OD_CONTROLWORD, 0, "u16", CW_ENABLE_OPERATION,
                   node=node, net=net)

    def disable_drive(self, node: Optional[int] = None,
                      net: Optional[int] = None) -> None:
        """Disable the drive output stage (controlword disable-voltage)."""
        self.write(OD_CONTROLWORD, 0, "u16", CW_DISABLE_VOLTAGE,
                   node=node, net=net)

    def set_velocity(self, velocity: int, node: Optional[int] = None,
                     net: Optional[int] = None) -> None:
        """Set target velocity (object 0x60FF, Profile Velocity mode)."""
        self.write(OD_TARGET_VELOCITY, 0, "i32", velocity, node=node, net=net)

    def set_target_position(self, position: int, node: Optional[int] = None, *,
                            relative: bool = False,
                            net: Optional[int] = None) -> None:
        """Set target position (0x607A) and trigger a Profile Position move.

        Toggles the controlword *new set-point* bit (bit 4) so the drive
        latches the new target. ``relative=True`` sets bit 6.
        """
        self.write(OD_TARGET_POSITION, 0, "i32", position, node=node, net=net)
        cw = CW_NEW_SETPOINT | (0x0040 if relative else 0)
        self.write(OD_CONTROLWORD, 0, "u16", cw, node=node, net=net)
        # drop the set-point bit again, leaving the drive enabled
        self.write(OD_CONTROLWORD, 0, "u16", CW_ENABLE_OPERATION,
                   node=node, net=net)

    def read_statusword(self, node: Optional[int] = None,
                        net: Optional[int] = None) -> int:
        """Read the DS402 statusword (object 0x6041)."""
        return self.read_int(OD_STATUSWORD, 0, "u16", node=node, net=net)

    def read_position(self, node: Optional[int] = None,
                      net: Optional[int] = None) -> int:
        """Read the actual position (object 0x6064)."""
        return self.read_int(OD_POSITION_ACTUAL, 0, "i32", node=node, net=net)

    def read_velocity(self, node: Optional[int] = None,
                      net: Optional[int] = None) -> int:
        """Read the actual velocity (object 0x606C)."""
        return self.read_int(OD_VELOCITY_ACTUAL, 0, "i32", node=node, net=net)

    def is_operation_enabled(self, node: Optional[int] = None,
                             net: Optional[int] = None) -> bool:
        """True if the drive reports *Operation enabled* and no fault."""
        sw = self.read_statusword(node=node, net=net)
        return bool(sw & SW_OPERATION_ENABLED) and not bool(sw & SW_FAULT)

    # -- CANopen DS401 digital I/O helpers (e.g. mcIO-K1) ------------------
    def set_outputs(self, mask: int, node: Optional[int] = None, *,
                    subindex: int = 1, net: Optional[int] = None) -> None:
        """Write an 8-bit digital output block (object 0x6200).

        For the mcIO-K1, ``subindex=1`` drives Dout0..3 (bits 0..3).
        """
        self.write(OD_DIGITAL_OUTPUTS, subindex, "u8", mask & 0xFF,
                   node=node, net=net)

    def read_outputs(self, node: Optional[int] = None, *,
                     subindex: int = 1, net: Optional[int] = None) -> int:
        """Read back a digital output block (object 0x6200)."""
        return self.read_int(OD_DIGITAL_OUTPUTS, subindex, "u8",
                             node=node, net=net)

    def read_inputs(self, node: Optional[int] = None, *,
                    subindex: int = 1, net: Optional[int] = None) -> int:
        """Read an 8-bit digital input block (object 0x6000).

        For the mcIO-K1: ``subindex=1`` = Din0..7, ``subindex=2`` = Din8..11.
        """
        return self.read_int(OD_DIGITAL_INPUTS, subindex, "u8",
                             node=node, net=net)

    # -- CANopen identity --------------------------------------------------
    def device_type(self, node: Optional[int] = None,
                    net: Optional[int] = None) -> int:
        """Read object 0x1000 (device type / supported profile)."""
        return self.read_int(OD_DEVICE_TYPE, 0, "u32", node=node, net=net)

    def identity(self, node: Optional[int] = None,
                 net: Optional[int] = None) -> dict:
        """Read the DS301 identity object (0x1018): vendor/product/rev/serial."""
        return {
            "vendor_id": self.read_int(OD_IDENTITY, 1, "u32", node=node, net=net),
            "product_code": self.read_int(OD_IDENTITY, 2, "u32",
                                          node=node, net=net),
            "revision": self.read_int(OD_IDENTITY, 3, "u32", node=node, net=net),
            "serial": self.read_int(OD_IDENTITY, 4, "u32", node=node, net=net),
        }

    # -- raw CAN -----------------------------------------------------------
    def send_frame(self, frame: CanFrame, net: Optional[int] = None) -> None:
        """Transmit a raw CAN frame via ``w m`` (or ``r m`` for RTR)."""
        cob = frame.can_id | (EXTENDED_FLAG if frame.extended else 0)
        net = self._net if net is None else net
        prefix = "" if net is None else f"{net} "
        if frame.rtr:
            body = f"{prefix}r m 0x{cob:X} {len(frame.data)}"
        else:
            tokens = [f"0x{b:02X}" for b in frame.data]
            body = f"{prefix}w m 0x{cob:X} {len(frame.data)}"
            if tokens:
                body += " " + " ".join(tokens)
        self._command(body.strip())

    def receive_frames(
        self, timeout: Optional[float] = None, max_frames: Optional[int] = None
    ) -> Iterator[CanFrame]:
        """Yield received CAN frames (``MSG`` notifications).

        First drains any frames buffered while awaiting command responses, then
        reads more from the port. Stops after ``timeout`` seconds of inactivity
        or after ``max_frames``.
        """
        count = 0
        while self._rx_frames:
            yield self._rx_frames.popleft()
            count += 1
            if max_frames is not None and count >= max_frames:
                return
        while True:
            raw = self._transport.read_line(timeout=timeout)
            if raw is None:
                return
            frame = self._parse_frame(raw.strip())
            if frame is None:
                continue
            yield frame
            count += 1
            if max_frames is not None and count >= max_frames:
                return

    @staticmethod
    def _parse_frame(line: str) -> Optional[CanFrame]:
        """Parse a ``[net] MSG cob_id len 0xXX ...`` notification line."""
        m = re.search(
            r"(?i)\bMSG\s+(0x[0-9A-Fa-f]+|\d+)\s+(\d+)\s*(.*)$", line)
        if not m:
            return None
        cob = int(m.group(1), 16) if m.group(1).lower().startswith("0x") \
            else int(m.group(1))
        dlc = int(m.group(2))
        rest = m.group(3).strip()
        byte_tokens = rest.split() if rest else []
        try:
            data = bytes(
                int(t, 16) if t.lower().startswith("0x") else int(t)
                for t in byte_tokens
            )[:dlc]
        except ValueError:
            return None
        extended = bool(cob & EXTENDED_FLAG)
        can_id = cob & (CAN_EXT_MASK if extended else CAN_STD_MASK)
        try:
            return CanFrame(can_id=can_id, data=data, extended=extended)
        except ValueError as exc:
            log.debug("Discarding malformed frame %r: %s", line, exc)
            return None
