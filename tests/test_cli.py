"""Offline tests for the CLI totem subcommands (io, fan).

The stick factory is monkey-patched to return a client backed by the in-memory
FakeTransport, so these assert the exact SDO wire strings without hardware.
"""
from __future__ import annotations

import argparse
import collections

from mican_stick2 import MiCanStick2
from mican_stick2 import cli


class FakeTransport:
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


def _patch(monkeypatch, responses):
    stick = MiCanStick2(port="/dev/null", use_sequence_numbers=False)
    fake = FakeTransport()
    fake.responses.extend(responses)
    stick._transport = fake
    monkeypatch.setattr(cli, "_make_stick", lambda args: stick)
    return stick, fake


def _args(**kw):
    ns = argparse.Namespace(node=0, bitrate=None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_cmd_io_backlight_on(monkeypatch):
    _, fake = _patch(monkeypatch, ["OK", "0xA5", "0x03", "0x08"])
    rc = cli.cmd_io(_args(set=None, backlight="on"))
    assert rc == 0
    assert fake.written[0] == "4 w 0x6200 1 u8 8"        # Dout3 on
    assert "4 r 0x6000 1 u8" in fake.written
    assert "4 r 0x6000 2 u8" in fake.written
    assert "4 r 0x6200 1 u8" in fake.written


def test_cmd_io_set_mask(monkeypatch):
    _, fake = _patch(monkeypatch, ["OK", "0x00", "0x00", "0x05"])
    rc = cli.cmd_io(_args(set="0x05", backlight=None))
    assert rc == 0
    assert fake.written[0] == "4 w 0x6200 1 u8 5"


def test_cmd_fan_start_with_speed(monkeypatch):
    # start(4) OK, enable_drive: 3 writes OK, set_velocity OK,
    # read_statusword, is_operation_enabled (read again), read_analog_input
    _, fake = _patch(monkeypatch, [
        "OK",                       # NMT start
        "OK", "OK", "OK",           # enable_drive (shutdown/switch-on/mode/enable) -> 4
        "OK",                       # actually 4th enable write
        "OK",                       # set_velocity
        "0x0027",                   # read_statusword
        "0x0027",                   # is_operation_enabled
        "2345",                     # read_analog_input
    ])
    rc = cli.cmd_fan(_args(start=True, stop=False, speed=30000,
                           reset_fault=False))
    assert rc == 0
    assert "1 start" in fake.written
    assert "1 w 0x60FF 0 i32 30000" in fake.written


def test_cmd_fan_stop(monkeypatch):
    _, fake = _patch(monkeypatch, ["OK", "OK", "0x0000", "0x0000", "100"])
    rc = cli.cmd_fan(_args(start=False, stop=True, speed=None,
                           reset_fault=False))
    assert rc == 0
    assert "1 w 0x60FF 0 i32 0" in fake.written      # velocity 0
    assert "1 w 0x6040 0 u16 0" in fake.written      # disable_drive
