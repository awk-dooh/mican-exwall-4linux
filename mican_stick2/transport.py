"""Self-healing serial transport for the miCAN-Stick2.

This layer knows nothing about the DS309-3 protocol. Its single responsibility
is to provide a reliable, thread-safe, line-oriented byte pipe on top of a USB
serial port that may disappear and reappear (unplug, USB reset, power glitch).

Robustness features:
    * Automatic (re)connect with capped exponential backoff.
    * Thread-safe send/receive via a re-entrant lock.
    * Per-operation timeouts.
    * Transparent recovery: a dropped port is reopened on the next operation
      instead of raising, unless the retry budget is exhausted.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import serial  # pyserial
except ImportError as exc:  # pragma: no cover - dependency hint
    raise ImportError(
        "pyserial is required. Install it with: pip install pyserial"
    ) from exc

log = logging.getLogger("mican_stick2.transport")


class TransportError(IOError):
    """Raised when the serial link cannot be established or used."""


@dataclass
class SerialConfig:
    """Serial line parameters.

    The miCAN-Stick2 exposes a USB Virtual COM Port (VCP), so the physical baud
    rate is effectively don't-care over USB; 115200 is a safe default. (The
    RS232 gateway variants default to 9600.) Lines are CR/LF terminated per the
    miControl DS309-3 command reference.
    """

    port: str
    baudrate: int = 115200
    bytesize: int = 8
    parity: str = "N"          # 'N', 'E', 'O', 'M', 'S'
    stopbits: float = 1
    read_timeout: float = 1.0   # seconds
    write_timeout: float = 2.0  # seconds
    # CiA 309-3 lines are CR/LF terminated (confirmed in AN1301).
    eol: bytes = b"\r\n"
    rtscts: bool = False
    dsrdtr: bool = False
    xonxoff: bool = False


class SerialTransport:
    """A reconnecting, thread-safe, line-oriented serial transport."""

    def __init__(
        self,
        config: SerialConfig,
        *,
        max_reconnect_attempts: int = 0,   # 0 = retry forever
        backoff_initial: float = 0.2,
        backoff_max: float = 5.0,
    ) -> None:
        self._cfg = config
        self._max_attempts = max_reconnect_attempts
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._ser: Optional["serial.Serial"] = None
        self._lock = threading.RLock()
        self._rx_buffer = bytearray()
        self._closed = False

    # -- lifecycle ---------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> None:
        """Open the port, retrying with backoff until it succeeds."""
        with self._lock:
            self._closed = False
            self._ensure_open()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._close_locked()

    def _close_locked(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
            finally:
                self._ser = None
        self._rx_buffer.clear()

    def _ensure_open(self) -> None:
        """(Re)open the port with capped exponential backoff. Caller holds lock."""
        if self.is_open:
            return
        if self._closed:
            raise TransportError("Transport has been closed.")

        attempt = 0
        backoff = self._backoff_initial
        last_err: Optional[Exception] = None
        while True:
            attempt += 1
            try:
                self._ser = serial.Serial(
                    port=self._cfg.port,
                    baudrate=self._cfg.baudrate,
                    bytesize=self._cfg.bytesize,
                    parity=self._cfg.parity,
                    stopbits=self._cfg.stopbits,
                    timeout=self._cfg.read_timeout,
                    write_timeout=self._cfg.write_timeout,
                    rtscts=self._cfg.rtscts,
                    dsrdtr=self._cfg.dsrdtr,
                    xonxoff=self._cfg.xonxoff,
                )
                # Flush any stale bytes from a previous session.
                self._ser.reset_input_buffer()
                self._ser.reset_output_buffer()
                self._rx_buffer.clear()
                log.info("Opened %s @ %d baud", self._cfg.port, self._cfg.baudrate)
                return
            except Exception as exc:  # noqa: BLE001 - normalized below
                last_err = exc
                self._close_locked()
                if self._max_attempts and attempt >= self._max_attempts:
                    raise TransportError(
                        f"Failed to open {self._cfg.port} after {attempt} "
                        f"attempts: {exc}"
                    ) from exc
                log.warning(
                    "Open %s failed (attempt %d): %s; retrying in %.1fs",
                    self._cfg.port, attempt, exc, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)

    # -- I/O ---------------------------------------------------------------
    def write_line(self, line: str) -> None:
        """Send one command line, appending the configured EOL."""
        payload = line.encode("ascii", errors="strict") + self._cfg.eol
        with self._lock:
            self._ensure_open()
            try:
                assert self._ser is not None
                self._ser.write(payload)
                self._ser.flush()
            except Exception as exc:  # noqa: BLE001 - reopen and surface
                log.warning("Write failed: %s; will reconnect", exc)
                self._close_locked()
                raise TransportError(f"Write failed: {exc}") from exc

    def read_line(self, timeout: Optional[float] = None) -> Optional[str]:
        """Read one EOL-terminated line.

        Returns the line without its terminator, or ``None`` on timeout.
        Reconnects transparently if the port drops mid-read.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        eol = self._cfg.eol
        with self._lock:
            while True:
                idx = self._rx_buffer.find(eol)
                if idx >= 0:
                    line = self._rx_buffer[:idx]
                    del self._rx_buffer[: idx + len(eol)]
                    return line.decode("ascii", errors="replace").rstrip("\r")

                if deadline is not None and time.monotonic() >= deadline:
                    return None

                self._ensure_open()
                try:
                    assert self._ser is not None
                    chunk = self._ser.read(256)
                except Exception as exc:  # noqa: BLE001 - reopen next loop
                    log.warning("Read failed: %s; will reconnect", exc)
                    self._close_locked()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TransportError(f"Read failed: {exc}") from exc
                    continue
                if chunk:
                    self._rx_buffer.extend(chunk)

    def reset_input(self) -> None:
        with self._lock:
            self._rx_buffer.clear()
            if self.is_open:
                try:
                    assert self._ser is not None
                    self._ser.reset_input_buffer()
                except Exception:  # noqa: BLE001
                    pass

    # -- context manager ---------------------------------------------------
    def __enter__(self) -> "SerialTransport":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
