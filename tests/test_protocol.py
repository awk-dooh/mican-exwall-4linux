"""Offline protocol tests for the miCAN-Stick2 client.

These run without any hardware by replacing the serial transport with an
in-memory fake. They verify that commands are formatted exactly as documented
in the miControl manual (AN1301) and that responses / notifications are parsed
correctly.

Run with:  pytest -q
"""
from __future__ import annotations

import collections

import pytest

from mican_stick2 import CanFrame, MiCanStick2
from mican_stick2.client import StickError


class FakeTransport:
    """Records written lines and returns pre-queued response lines."""

    def __init__(self):
        self.written = []
        self.responses = collections.deque()

    def open(self):
        pass

    def close(self):
        pass

    def reset_input(self):
        pass

    def write_line(self, line):
        self.written.append(line)

    def read_line(self, timeout=None):
        if self.responses:
            return self.responses.popleft()
        return None


def make(**kwargs):
    stick = MiCanStick2(port="/dev/null", use_sequence_numbers=False, **kwargs)
    fake = FakeTransport()
    stick._transport = fake
    return stick, fake


# -- command formatting ----------------------------------------------------
def test_info_version_command_and_parse():
    stick, fake = make()
    fake.responses.append("0x0139 0x15010001 1.92.03.00 0 128 1.10 0.0")
    info = stick.version_info()
    assert fake.written == ["info version"]
    assert info["vendor_id"] == "0x0139"
    assert info["firmware_version"] == "1.92.03.00"
    assert info["protocol_version"] == "1.10"


def test_set_bitrate_uses_kbit():
    stick, fake = make()
    fake.responses.append("OK")
    stick.set_bitrate(500_000)
    assert fake.written == ["init 500"]


def test_set_bitrate_rejects_unsupported():
    stick, _ = make()
    with pytest.raises(ValueError):
        stick.set_bitrate(333_000)


def test_nmt_start_broadcast():
    stick, fake = make(node=0)
    fake.responses.append("OK")
    stick.start()
    assert fake.written == ["0 start"]


def test_nmt_start_specific_node():
    stick, fake = make()
    fake.responses.append("OK")
    stick.start(node=5)
    assert fake.written == ["5 start"]


def test_send_standard_frame():
    stick, fake = make()
    fake.responses.append("OK")
    stick.send_frame(CanFrame(can_id=0x201, data=bytes([0x11, 0x22])))
    assert fake.written == ["w m 0x201 2 0x11 0x22"]


def test_send_extended_frame_sets_flag():
    stick, fake = make()
    fake.responses.append("OK")
    stick.send_frame(CanFrame(can_id=0x201, data=bytes([0x11, 0x22]),
                              extended=True))
    # 0x201 | 0x20000000 == 0x20000201
    assert fake.written == ["w m 0x20000201 2 0x11 0x22"]


def test_send_empty_data():
    stick, fake = make()
    fake.responses.append("OK")
    stick.send_frame(CanFrame(can_id=0x100))
    assert fake.written == ["w m 0x100 0"]


def test_send_rtr_uses_read_msg():
    stick, fake = make()
    fake.responses.append("OK")
    stick.send_frame(CanFrame(can_id=0x181, rtr=True))
    assert fake.written == ["r m 0x181 0"]


def test_sdo_read_write():
    stick, fake = make()
    fake.responses.append("0x2545")
    val = stick.read(0x3020, 0, "xi32", node=5)
    assert fake.written[-1] == "5 r 0x3020 0 xi32"
    assert val == "0x2545"

    fake.responses.append("OK")
    stick.write(0x3300, 0, "i32", -1000, node=5)
    assert fake.written[-1] == "5 w 0x3300 0 i32 -1000"


# -- error handling --------------------------------------------------------
def test_error_response_raises():
    stick, fake = make()
    fake.responses.append("Error 101")
    with pytest.raises(StickError) as ei:
        stick.version()
    assert ei.value.code == 101


# -- notification parsing --------------------------------------------------
def test_parse_msg_notification_standard():
    frame = MiCanStick2._parse_frame(
        "MSG 0x00000185 8 0x50 0x00 0x00 0x00 0x00 0x00 0xF6 0x00")
    assert frame is not None
    assert frame.can_id == 0x185
    assert frame.extended is False
    assert frame.data == bytes([0x50, 0, 0, 0, 0, 0, 0xF6, 0])


def test_parse_msg_notification_extended():
    frame = MiCanStick2._parse_frame("MSG 0x20000181 1 0x11")
    assert frame is not None
    assert frame.extended is True
    assert frame.can_id == 0x181
    assert frame.data == bytes([0x11])


def test_parse_non_frame_returns_none():
    assert MiCanStick2._parse_frame("OK") is None
    assert MiCanStick2._parse_frame("Error 100") is None


def test_buffered_frame_during_command_is_yielded_later():
    stick, fake = make()
    # start returns OK, but a boot-up MSG arrives first.
    fake.responses.append("MSG 0x00000185 1 0x10")
    fake.responses.append("OK")
    stick.start(node=5)
    frames = list(stick.receive_frames(timeout=0))
    assert len(frames) == 1
    assert frames[0].can_id == 0x185


# -- CanFrame validation ---------------------------------------------------
def test_canframe_rejects_oversize_std_id():
    with pytest.raises(ValueError):
        CanFrame(can_id=0x800)  # > 0x7FF without extended


def test_canframe_rejects_long_data():
    with pytest.raises(ValueError):
        CanFrame(can_id=0x1, data=bytes(9))


# -- CANopen DS402 drive helpers (mcDSA-E60, node 1) -----------------------
def test_read_int_parses_hex_and_decimal():
    stick, fake = make()
    fake.responses.append("0x000F")
    assert stick.read_int(0x6041, 0, "u16", node=1) == 0x0F
    fake.responses.append("1234")
    assert stick.read_int(0x6064, 0, "i32", node=1) == 1234


def test_enable_drive_walks_state_machine():
    stick, fake = make()
    for _ in range(4):
        fake.responses.append("OK")
    stick.enable_drive(node=1, mode=3)
    assert fake.written == [
        "1 w 0x6040 0 u16 6",     # shutdown
        "1 w 0x6040 0 u16 7",     # switch on
        "1 w 0x6060 0 i8 3",      # profile velocity mode
        "1 w 0x6040 0 u16 15",    # enable operation
    ]


def test_enable_drive_can_skip_mode():
    stick, fake = make()
    for _ in range(3):
        fake.responses.append("OK")
    stick.enable_drive(node=1, mode=None)
    assert fake.written == [
        "1 w 0x6040 0 u16 6",
        "1 w 0x6040 0 u16 7",
        "1 w 0x6040 0 u16 15",
    ]


def test_set_velocity_writes_target_velocity():
    stick, fake = make()
    fake.responses.append("OK")
    stick.set_velocity(50_000, node=1)
    assert fake.written == ["1 w 0x60FF 0 i32 50000"]


def test_read_statusword_and_position():
    stick, fake = make()
    fake.responses.append("0x0637")
    assert stick.read_statusword(node=1) == 0x0637
    assert fake.written[-1] == "1 r 0x6041 0 u16"
    fake.responses.append("-250")
    assert stick.read_position(node=1) == -250
    assert fake.written[-1] == "1 r 0x6064 0 i32"


def test_is_operation_enabled():
    stick, fake = make()
    fake.responses.append("0x0027")  # bits 0,1,2 set, no fault
    assert stick.is_operation_enabled(node=1) is True
    fake.responses.append("0x0008")  # fault bit set
    assert stick.is_operation_enabled(node=1) is False


# -- CANopen DS401 I/O helpers (mcIO-K1, node 4) ---------------------------
def test_set_outputs_writes_8bit_block():
    stick, fake = make()
    fake.responses.append("OK")
    stick.set_outputs(0x01, node=4)
    assert fake.written == ["4 w 0x6200 1 u8 1"]


def test_read_inputs_low_and_high():
    stick, fake = make()
    fake.responses.append("0xA5")
    assert stick.read_inputs(node=4) == 0xA5
    assert fake.written[-1] == "4 r 0x6000 1 u8"
    fake.responses.append("0x03")
    assert stick.read_inputs(node=4, subindex=2) == 0x03
    assert fake.written[-1] == "4 r 0x6000 2 u8"


def test_identity_reads_1018():
    stick, fake = make()
    fake.responses.extend(["0x139", "0x15010001", "0x10203", "0x42"])
    ident = stick.identity(node=1)
    assert ident == {
        "vendor_id": 0x139,
        "product_code": 0x15010001,
        "revision": 0x10203,
        "serial": 0x42,
    }
    assert fake.written[-4:] == [
        "1 r 0x1018 1 u32",
        "1 r 0x1018 2 u32",
        "1 r 0x1018 3 u32",
        "1 r 0x1018 4 u32",
    ]
