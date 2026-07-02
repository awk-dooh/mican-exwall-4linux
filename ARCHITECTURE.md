# Solution overview — how `mican-exwall-4linux` works

This document explains **what the software does, how the pieces fit together, and
what happens end-to-end** when you talk to a miCAN-Stick2 from Linux. It
complements the [README](README.md) (which is the practical how-to).

---

## 1. The core idea in one paragraph

The miCAN-Stick2 is a **USB↔CAN gateway that speaks ASCII text over a virtual
serial port**. There is no special kernel driver on Linux — the stick shows up
as `/dev/ttyUSB0` (or `/dev/mican0` after our udev rule). This project is the
**user-space software** that opens that serial port, sends the stick's DS309-3
text commands, parses its replies, and turns everything into a clean Python API,
a CLI, and — optionally — a normal Linux SocketCAN interface.

---

## 2. Layered architecture

The code is deliberately split into thin layers, each with a single job. Higher
layers depend only on the layer directly beneath them.

```
┌─────────────────────────────────────────────────────────────┐
│  CLI  (mican_stick2/cli.py)        Library users' own code   │
│  detect · version · monitor · send · bridge                  │
└───────────────┬───────────────────────────┬─────────────────┘
                │                            │
                ▼                            ▼
┌──────────────────────────┐   ┌────────────────────────────────┐
│ SocketCAN bridge         │   │  Client / protocol layer        │
│ (bridge.py)              │   │  (client.py)                    │
│ vcan0  ⇄  CanFrame       │   │  DS309-3 ASCII commands,        │
│ needs python-can         │   │  CanFrame model, error mapping  │
└───────────┬──────────────┘   └───────────────┬────────────────┘
            │                                   │
            └───────────────┬───────────────────┘
                            ▼
              ┌────────────────────────────────┐
              │  Serial transport (transport.py)│
              │  reconnect · locking · timeouts │
              └───────────────┬────────────────┘
                              ▼
              ┌────────────────────────────────┐
              │  pyserial  →  /dev/mican0       │
              │  (kernel ftdi_sio / cdc_acm)    │
              └───────────────┬────────────────┘
                              ▼
                      ░ miCAN-Stick2 ░  ──CAN──▶  bus / drives

  Discovery (discovery.py) feeds the port name into the transport.
```

### Responsibilities per layer

| Layer | File | Responsibility | Knows about… |
|-------|------|----------------|--------------|
| **Discovery** | `discovery.py` | Find the right `/dev/...` node | USB VID/PID, udev symlink |
| **Transport** | `transport.py` | Reliable line-oriented byte pipe | serial ports, reconnection |
| **Client** | `client.py` | DS309-3 command/response + `CanFrame` | the miControl text protocol |
| **Bridge** | `bridge.py` | Map to Linux SocketCAN | `python-can`, `vcan` |
| **CLI** | `cli.py` | Human-friendly commands | argument parsing, output |

Because each layer is isolated, if miControl ever changes a command's spelling
you only touch `client.py`; the robust transport, discovery, bridge and CLI stay
untouched.

---

## 3. The layers in detail

### 3.1 Discovery (`discovery.py`)

Finds which serial device is the stick, so users rarely need `--port`:

1. If the udev rule created **`/dev/mican0`**, use it (rock-solid, name never
   changes between reboots).
2. Otherwise score every serial port by USB VID/PID and manufacturer string and
   pick the best match.
3. Otherwise, if there's exactly one USB serial port, use that.

`MICAN_STICK2_VIDPID=0403:6001` makes matching exact once you've confirmed your
stick's IDs with `lsusb`.

### 3.2 Serial transport (`transport.py`) — the robustness core

This is what makes the solution *"infallible"* in practice. It provides a
thread-safe, line-oriented pipe with **automatic recovery**:

- **Auto-reconnect** with capped exponential backoff. If the USB stick is
  unplugged and replugged, the next read/write transparently reopens the port
  instead of crashing.
- **Thread-safe** via a re-entrant lock — one client can be shared across
  threads (the bridge relies on this).
- **Per-operation timeouts** so nothing blocks forever.
- **Line buffering** with a configurable EOL (`CR LF` for this device).

It knows nothing about CAN or DS309-3 — just bytes and lines.

### 3.3 Client / protocol (`client.py`) — the brains

Implements the miControl DS309-3 ASCII conversation and exposes an ergonomic
API. Key responsibilities:

- **Command formatting** — e.g. `set_bitrate(500_000)` becomes the wire line
  `init 500` (the device counts in kbit/s); `send_frame(...)` becomes
  `w m 0x201 2 0x11 0x22`.
- **Response parsing** — `OK`, `Error <code>`, or data; errors raise a typed
  `StickError` with a human-readable description.
- **Sequence numbers** — each request can carry `[seq]`; replies are matched to
  their request so stale/async lines are ignored.
- **Retries** — transient transport failures are retried automatically.
- **Asynchronous frame handling** — received CAN messages arrive **unsolicited**
  as `MSG` lines, possibly *in the middle of* a command's reply (e.g. `start`
  returns `OK` but also triggers boot-up `MSG`s). The client buffers those
  frames so they're delivered later via `receive_frames()` and never lost.
- **`CanFrame`** — a small validated dataclass (standard/extended id, ≤ 8 data
  bytes, RTR) that both directions use.

### 3.4 SocketCAN bridge (`bridge.py`) — optional interop

Turns the stick into a *normal* Linux CAN interface so existing tooling works.
Two threads run concurrently:

- **stick → vcan:** `receive_frames()` → `python-can` messages injected into
  `vcan0` (so `candump vcan0` shows live traffic).
- **vcan → stick:** frames written to `vcan0` (`cansend`, `python-can`) →
  `send_frame()` on the real bus.

This is opt-in and only needs `python-can`; the rest of the package works
without it.

### 3.5 CLI (`cli.py`)

A thin `argparse` front-end that wires the above into five subcommands:
`detect`, `version`, `monitor`, `send`, `bridge`. Installed as the **`mican`**
console command via `pyproject.toml`.

---

## 4. End-to-end walk-throughs

### 4.1 `mican version`

```
CLI parses args
  └─ discovery.find_stick_port() ─────────────▶ "/dev/mican0"
       └─ MiCanStick2(port).version()
            └─ client formats  "info version"
                 └─ transport.write_line("info version\r\n")   → stick
                 ◀─ transport.read_line()  "0x0139 0x150... 1.10 0.0"
            ◀─ parsed version string
  prints it
```

### 4.2 `mican monitor --bitrate 500000`

```
set_bitrate(500000) → "init 500\r\n"      ◀ "OK"
start(node=0)       → "0 start\r\n"        ◀ "OK"  (+ boot-up MSG lines buffered)
loop receive_frames():
    transport.read_line()  "MSG 0x185 8 0x50 ..."
      └─ client._parse_frame() → CanFrame(id=0x185, data=…)
    print "STD 0x185 [8] 50 00 …"
```

### 4.3 `mican send --id 0x201 --data 1122`

```
CanFrame(0x201, b"\x11\x22")
  └─ send_frame() → "w m 0x201 2 0x11 0x22\r\n"   ◀ "OK"
```

### 4.4 Bridge data flow

```
   Real CAN bus ──▶ stick ──"MSG …"──▶ transport ──▶ client ──CanFrame──▶ bridge ──▶ vcan0 ──▶ candump
   cansend ──▶ vcan0 ──▶ bridge ──CanFrame──▶ client ──"w m …"──▶ transport ──▶ stick ──▶ Real CAN bus
```

---

## 5. Design decisions & rationale

| Decision | Why |
|----------|-----|
| Serial client as the foundation, SocketCAN as an optional layer | The stick *only* speaks DS309-3 over serial; anything "native" must be built on top of that anyway. |
| Self-healing transport separate from protocol | Robustness (reconnect/locking/timeouts) is reusable and independently testable; protocol quirks stay isolated. |
| Buffer async `MSG` frames during command replies | The firmware interleaves unsolicited frames with responses; without buffering, frames would be lost or misparsed. |
| Typed exceptions (`TransportError`, `StickError`, `ProtocolError`) | Callers can distinguish "cable fell out" from "device said no". |
| Bitrate API in bit/s, converted to kbit/s internally | Friendlier, less error-prone API; the wire format detail is hidden. |
| udev rule → `/dev/mican0` | Stable device name and non-root access; removes the #1 beginner pitfall. |
| Everything configurable (`SerialConfig`, `_fmt_*`) | If firmware dialect differs, adaptation is a one-line change, not a rewrite. |

---

## 6. What is verified vs. assumed

- **Verified against miControl docs** (*AN1301* + miCAN-Stick2 technical data):
  the command set, response/notification syntax, bitrate encoding, error codes,
  CR/LF framing, pinout, LEDs, and terminator behaviour. These are covered by
  offline tests in [`tests/test_protocol.py`](tests/test_protocol.py).
- **Assumed / configurable:** the exact USB VID:PID (defaults to common FTDI IDs;
  confirm with `lsusb`) and the VCP baud rate (irrelevant for a USB CDC/VCP but
  exposed for completeness).

---

## 7. Testing without hardware

The protocol layer is fully testable offline by swapping the transport for an
in-memory fake that records written lines and returns canned responses. This
proves the exact bytes on the wire match the manual — run:

```bash
source .venv/bin/activate
pytest -q
```

---

## 8. Extending the solution

- **New command?** Add a small method in `client.py` that formats the line and
  calls `_command(...)`. Add a test asserting the exact wire string.
- **New CLI verb?** Add a subparser + handler in `cli.py`.
- **Different framing/baud?** Adjust `SerialConfig` in `transport.py`.
- **Real (non-virtual) SocketCAN?** Point `setup_vcan.sh`/`--can` at a physical
  `canX` interface; the bridge code is identical.
```
