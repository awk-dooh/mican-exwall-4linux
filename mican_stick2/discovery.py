"""USB port auto-detection for the miCAN-Stick2.

The stick enumerates as a USB CDC/FTDI virtual serial port. This module lists
candidate ``/dev/ttyUSB*`` / ``/dev/ttyACM*`` devices and, where possible,
filters them by USB vendor/product metadata so the caller does not have to
hard-code a device node that can change between reboots.

Set ``MICAN_STICK2_VIDPID`` (e.g. ``0403:6001``) once you have confirmed the
real IDs from ``lsusb`` to make detection exact.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

try:
    from serial.tools import list_ports
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "pyserial is required. Install it with: pip install pyserial"
    ) from exc


# Known-plausible identifiers. FTDI (0403:6001) is the most common bridge chip
# for this class of device; confirm the real value with `lsusb` and add it here
# or via the MICAN_STICK2_VIDPID environment variable for an exact match.
_KNOWN_VIDPIDS = {
    (0x0403, 0x6001),  # FTDI FT232 (typical USB-serial bridge)
    (0x0403, 0x6015),  # FTDI FT230X/FT231X
    (0x1cbe, 0x0003),  # generic CDC (placeholder; adjust to confirmed value)
}

_MANUFACTURER_HINTS = ("micontrol", "mican", "mi-can", "micontrol gmbh")


@dataclass
class PortInfo:
    device: str
    description: str
    vid: Optional[int]
    pid: Optional[int]
    serial_number: Optional[str]
    manufacturer: Optional[str]

    @property
    def vidpid(self) -> Optional[str]:
        if self.vid is None or self.pid is None:
            return None
        return f"{self.vid:04x}:{self.pid:04x}"


def _env_vidpid() -> Optional[tuple[int, int]]:
    raw = os.environ.get("MICAN_STICK2_VIDPID")
    if not raw:
        return None
    try:
        vid_s, pid_s = raw.lower().replace("0x", "").split(":")
        return int(vid_s, 16), int(pid_s, 16)
    except (ValueError, AttributeError):
        return None


def list_candidate_ports() -> List[PortInfo]:
    """Return all serial ports as :class:`PortInfo`, best matches first."""
    infos: List[PortInfo] = []
    for p in list_ports.comports():
        infos.append(
            PortInfo(
                device=p.device,
                description=p.description or "",
                vid=p.vid,
                pid=p.pid,
                serial_number=p.serial_number,
                manufacturer=getattr(p, "manufacturer", None),
            )
        )
    infos.sort(key=_match_score, reverse=True)
    return infos


def _match_score(info: PortInfo) -> int:
    score = 0
    env = _env_vidpid()
    if env and info.vid == env[0] and info.pid == env[1]:
        score += 100
    if info.vid is not None and (info.vid, info.pid) in _KNOWN_VIDPIDS:
        score += 40
    hay = f"{info.description} {info.manufacturer or ''}".lower()
    if any(h in hay for h in _MANUFACTURER_HINTS):
        score += 60
    # Prefer real USB serial nodes over onboard/virtual ports.
    if "ttyusb" in info.device.lower() or "ttyacm" in info.device.lower():
        score += 10
    return score


def find_stick_port(*, required: bool = False) -> Optional[str]:
    """Return the most likely miCAN-Stick2 device node, or ``None``.

    Detection order:
        1. The stable ``/dev/mican0`` symlink created by the bundled udev rule.
        2. A port that positively matches by VID/PID or manufacturer string.
        3. If exactly one USB serial port exists, that single port.

    :param required: if True, raise ``RuntimeError`` when no port is found.
    """
    # 1. Stable symlink from the installed udev rule.
    if os.path.exists("/dev/mican0"):
        return "/dev/mican0"

    candidates = list_candidate_ports()
    if candidates and _match_score(candidates[0]) >= 40:
        return candidates[0].device

    usb_serial = [
        c for c in candidates
        if "ttyusb" in c.device.lower() or "ttyacm" in c.device.lower()
    ]
    if len(usb_serial) == 1:
        return usb_serial[0].device

    if required:
        detail = ", ".join(
            f"{c.device} ({c.vidpid or '?'} {c.description})" for c in candidates
        ) or "none"
        raise RuntimeError(
            "Could not identify the miCAN-Stick2 port automatically. "
            f"Candidates: {detail}. Set MICAN_STICK2_VIDPID or pass port=... "
            "explicitly."
        )
    return None
