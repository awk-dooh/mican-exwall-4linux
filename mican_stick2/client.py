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

# --- mcDSA-Exx manufacturer-specific objects (0x3000 profile) -------------
# Source: decompiled mcManual (content_mcDSA-Exx_Parameter 3000h). Only the
# commonly needed commissioning objects are named here; see CANOPEN.md.
OD_DEV_CMD = 0x3000            # u8  device command (see DEV_CMD_*)
OD_DEV_ENABLE = 0x3004        # u8  output-stage enable {0,1} (native)
OD_CURR_KP = 0x3210           # i32 current controller P
OD_CURR_KI = 0x3211           # i32 current controller I
OD_CURR_LIMIT_MAX_POS = 0x3221  # u32 current limit, positive
OD_CURR_LIMIT_MAX_NEG = 0x3223  # u32 current limit, negative
OD_VEL_KP = 0x3310            # i32 velocity/position controller P
OD_VEL_KI = 0x3311            # i32 velocity/position controller I
OD_VEL_KD = 0x3312            # i32 velocity/position controller D
OD_VEL_KVFF = 0x3314          # u16 velocity feed-forward [0..2000]
OD_VEL_FEEDBACK = 0x3350      # u32 velocity feedback source
OD_SVEL_FEEDBACK = 0x3550     # u32 SVel feedback source
OD_PWM_FREQUENCY = 0x3830     # u32 PWM frequency (Hz)
OD_MOTOR_TYPE = 0x3900        # u8  0=DC, 1=BLDC
OD_MOTOR_NN = 0x3901          # u16 nominal speed [0..30000] rpm
OD_MOTOR_UN = 0x3902          # u16 nominal voltage [0..60000]
OD_MOTOR_POLN = 0x3910        # u8  pole count [1..100]
OD_MOTOR_POLARITY = 0x3911    # u16 motor polarity bitfield
OD_MOTOR_ENC_RESOLUTION = 0x3962  # u32 encoder resolution (increments)
OD_MPU_CMD = 0x5000           # i16 MPU program command (see MPU_CMD_*)
OD_ANALOG_INPUT = 0x3100      # i16 IO_AIN0 (mV); channel n = 0x3100 + n

# DEV_Cmd (0x3000) command values
DEV_CMD_NOP = 0x00
DEV_CMD_CLEAR_ERROR = 0x01
DEV_CMD_QUICK_STOP = 0x02
DEV_CMD_HALT = 0x03
DEV_CMD_CONTINUE = 0x04
DEV_CMD_UPDATE = 0x05
DEV_CMD_STORE_PARAM = 0x80     # save 0x3000-range params to EEPROM (slow!)
DEV_CMD_RESTORE_PARAM = 0x81   # reload params from EEPROM
DEV_CMD_DEFAULT_PARAM = 0x82   # reset params to defaults (RAM only)
DEV_CMD_CLEAR_PARAM = 0x83     # default + store

# MPU_Cmd (0x5000) program command values
MPU_CMD_CLEAR_ERROR = 0x01
MPU_CMD_START = 0x02
MPU_CMD_BREAK = 0x03
MPU_CMD_CONTINUE = 0x04
MPU_CMD_STORE = 0x80
MPU_CMD_RESTORE = 0x81

# MOTOR_Type (0x3900) values
MOTOR_TYPE_DC = 0
MOTOR_TYPE_BLDC = 1

# PWM_Frequency (0x3830) permitted values, in Hz
# (100000/200000 need firmware >= 1.93.00.CD; 200000 only mcDSA-E65)
PWM_FREQUENCIES = (12500, 25000, 32000, 50000, 100000, 200000)


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
             node: Optional[int] = None, net: Optional[int] = None,
             timeout: Optional[float] = None) -> str:
        """``[[net] node] r <index> <subindex> <datatype>`` (SDO read)."""
        node = self._node if node is None else node
        body = f"{self._addr(node, net)} r 0x{index:X} {subindex} {datatype}"
        return self._command(body.strip(), timeout=timeout)

    def write(self, index: int, subindex: int, datatype: str, value: int,
              node: Optional[int] = None, net: Optional[int] = None,
              timeout: Optional[float] = None) -> None:
        """``[[net] node] w <index> <subindex> <datatype> <value>`` (SDO write)."""
        node = self._node if node is None else node
        body = (f"{self._addr(node, net)} w 0x{index:X} {subindex} "
                f"{datatype} {value}")
        self._command(body.strip(), timeout=timeout)

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

    # -- mcDSA-Exx manufacturer-specific (0x3000 profile) ------------------
    # Typed helpers for the servo commissioning objects. These target the
    # miControl-native parameter set (see CANOPEN.md), complementing the
    # generic DS402 helpers above.

    def device_command(self, command: int, node: Optional[int] = None, *,
                       net: Optional[int] = None,
                       timeout: Optional[float] = None) -> None:
        """Execute a DEV_Cmd (object 0x3000) — see ``DEV_CMD_*`` constants."""
        self.write(OD_DEV_CMD, 0, "u8", command, node=node, net=net,
                   timeout=timeout)

    def clear_error(self, node: Optional[int] = None,
                    net: Optional[int] = None) -> None:
        """Clear a drive error (DEV_Cmd CMD_ClearError)."""
        self.device_command(DEV_CMD_CLEAR_ERROR, node=node, net=net)

    def quick_stop(self, node: Optional[int] = None,
                   net: Optional[int] = None) -> None:
        """Quick-stop the motor along the quick-stop ramp (DEV_Cmd)."""
        self.device_command(DEV_CMD_QUICK_STOP, node=node, net=net)

    def halt(self, node: Optional[int] = None,
             net: Optional[int] = None) -> None:
        """Halt the motor along the normal ramp (DEV_Cmd)."""
        self.device_command(DEV_CMD_HALT, node=node, net=net)

    def continue_motion(self, node: Optional[int] = None,
                        net: Optional[int] = None) -> None:
        """Resume motion after halt/quick-stop (DEV_Cmd CMD_Continue)."""
        self.device_command(DEV_CMD_CONTINUE, node=node, net=net)

    def update_setpoints(self, node: Optional[int] = None,
                         net: Optional[int] = None) -> None:
        """Apply new motion setpoints (DEV_Cmd CMD_Update)."""
        self.device_command(DEV_CMD_UPDATE, node=node, net=net)

    def enable_output(self, on: bool = True, node: Optional[int] = None,
                      net: Optional[int] = None) -> None:
        """Enable/disable the output stage via native DEV_Enable (0x3004)."""
        self.write(OD_DEV_ENABLE, 0, "u8", 1 if on else 0, node=node, net=net)

    def store_parameters(self, node: Optional[int] = None, *,
                         net: Optional[int] = None,
                         timeout: float = 8.0) -> None:
        """Persist the 0x3000-range parameters to EEPROM (takes seconds)."""
        self.device_command(DEV_CMD_STORE_PARAM, node=node, net=net,
                            timeout=timeout)

    def restore_parameters(self, node: Optional[int] = None, *,
                           net: Optional[int] = None,
                           timeout: float = 8.0) -> None:
        """Reload parameters from EEPROM (takes seconds)."""
        self.device_command(DEV_CMD_RESTORE_PARAM, node=node, net=net,
                            timeout=timeout)

    def default_parameters(self, node: Optional[int] = None, *,
                           net: Optional[int] = None,
                           timeout: float = 8.0) -> None:
        """Reset parameters to defaults in RAM (not stored; takes seconds)."""
        self.device_command(DEV_CMD_DEFAULT_PARAM, node=node, net=net,
                            timeout=timeout)

    def set_pwm_frequency(self, hz: int, node: Optional[int] = None,
                          net: Optional[int] = None) -> None:
        """Set PWM frequency (object 0x3830). Only allowed when disabled."""
        if hz not in PWM_FREQUENCIES:
            raise ValueError(
                f"Unsupported PWM frequency {hz} Hz; choose from "
                f"{PWM_FREQUENCIES}")
        self.write(OD_PWM_FREQUENCY, 0, "u32", hz, node=node, net=net)

    def set_current_limits(self, positive: int, negative: Optional[int] = None,
                           node: Optional[int] = None,
                           net: Optional[int] = None) -> None:
        """Set the max current limits (objects 0x3221 / 0x3223).

        The device-specific maximum applies (see the drive's technical data).
        If ``negative`` is omitted, ``positive`` is used for both directions.
        """
        if negative is None:
            negative = positive
        self.write(OD_CURR_LIMIT_MAX_POS, 0, "u32", positive, node=node, net=net)
        self.write(OD_CURR_LIMIT_MAX_NEG, 0, "u32", negative, node=node, net=net)

    def set_current_gains(self, kp: int, ki: int, node: Optional[int] = None,
                          net: Optional[int] = None) -> None:
        """Set the PI current-controller gains (objects 0x3210 / 0x3211)."""
        self.write(OD_CURR_KP, 0, "i32", kp, node=node, net=net)
        self.write(OD_CURR_KI, 0, "i32", ki, node=node, net=net)

    def set_velocity_gains(self, kp: int, ki: int,
                           kd: Optional[int] = None,
                           kvff: Optional[int] = None,
                           node: Optional[int] = None,
                           net: Optional[int] = None) -> None:
        """Set the PID velocity/position-controller gains (0x3310..0x3314)."""
        self.write(OD_VEL_KP, 0, "i32", kp, node=node, net=net)
        self.write(OD_VEL_KI, 0, "i32", ki, node=node, net=net)
        if kd is not None:
            self.write(OD_VEL_KD, 0, "i32", kd, node=node, net=net)
        if kvff is not None:
            self.write(OD_VEL_KVFF, 0, "u16", kvff, node=node, net=net)

    def read_analog_input(self, channel: int = 0, node: Optional[int] = None,
                          net: Optional[int] = None) -> int:
        """Read an analog input in millivolts (IO_AIN, object 0x3100 + channel).

        On the totem's mcDSA-E60, channel 0 is the 10 kΩ NTC temperature sensor.
        Returns a signed value in mV (range -10000..10000).
        """
        return self.read_int(OD_ANALOG_INPUT + channel, 0, "i16",
                             node=node, net=net)

    def configure_motor(self, *, motor_type: Optional[int] = None,
                        pole_count: Optional[int] = None,
                        nominal_speed: Optional[int] = None,
                        nominal_voltage: Optional[int] = None,
                        encoder_resolution: Optional[int] = None,
                        polarity: Optional[int] = None,
                        node: Optional[int] = None,
                        net: Optional[int] = None) -> None:
        """Write the basic motor parameters (only the given ones).

        Maps to MOTOR_Type (0x3900), MOTOR_PolN (0x3910), MOTOR_Nn (0x3901),
        MOTOR_Un (0x3902), MOTOR_ENC_Resolution (0x3962), MOTOR_Polarity
        (0x3911). Change these only while the output stage is disabled.
        """
        if motor_type is not None:
            self.write(OD_MOTOR_TYPE, 0, "u8", motor_type, node=node, net=net)
        if pole_count is not None:
            self.write(OD_MOTOR_POLN, 0, "u8", pole_count, node=node, net=net)
        if nominal_speed is not None:
            self.write(OD_MOTOR_NN, 0, "u16", nominal_speed, node=node, net=net)
        if nominal_voltage is not None:
            self.write(OD_MOTOR_UN, 0, "u16", nominal_voltage,
                       node=node, net=net)
        if encoder_resolution is not None:
            self.write(OD_MOTOR_ENC_RESOLUTION, 0, "u32", encoder_resolution,
                       node=node, net=net)
        if polarity is not None:
            self.write(OD_MOTOR_POLARITY, 0, "u16", polarity, node=node, net=net)

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
