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
