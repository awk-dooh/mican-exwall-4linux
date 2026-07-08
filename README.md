# mican-exwall-4linux

A **robust, self-healing Linux driver, command-line tool and SocketCAN bridge**
for the **miControl miCAN-Stick2** USB-CAN gateway.

Built and verified against the official miControl documentation
(*AN1301 "miControl Text-Kommandos"* and the *miCAN-Stick2* technical data).

---

## Table of contents

1. [What this is](#1-what-this-is)
2. [How the miCAN-Stick2 works](#2-how-the-mican-stick2-works)
3. [Hardware: wiring, pinout, LEDs, terminator](#3-hardware-wiring-pinout-leds-terminator)
4. [Monkey-proof installation](#4-monkey-proof-installation)
5. [First contact: talk to the stick](#5-first-contact-talk-to-the-stick)
6. [Command-line usage](#6-command-line-usage)
7. [Use it as a Python library](#7-use-it-as-a-python-library)
8. [SocketCAN bridge (candump / cansend / python-can)](#8-socketcan-bridge)
9. [Troubleshooting](#9-troubleshooting)
10. [Protocol reference](#10-protocol-reference)
11. [Project layout](#11-project-layout)

---

## 1. What this is

The miCAN-Stick2 lets a PC send and receive CAN / CANopen messages. On Windows
it needs miControl's driver; **on Linux it works with the drivers already built
into the kernel** — this project provides the user-space software to actually
talk to it.

You get three things:

- a **Python library** (`mican_stick2`) with a clean, reliable API,
- a **command-line tool** (`mican`) for quick tasks (detect, version, monitor,
  send, bridge),
- an optional **SocketCAN bridge** so standard tools (`candump`, `cansend`,
  `python-can`) can use the stick as if it were a native CAN interface.

> **Controlling the totem devices?** See [CANOPEN.md](CANOPEN.md) for the
> ready-made helpers to drive the **mcDSA-E60 servo** (Node 1) and the
> **mcIO-K1 digital I/O** (Node 4) that hang off the stick in the Exwall totem.
>
> **New to this hardware?** Start with
> [MICONTROL_4_DUMMIES.md](MICONTROL_4_DUMMIES.md) — a plain-language guide to
> what the totem's miControl devices are, how they're wired, and runnable
> examples (switch the backlight, read a sensor, spin the fan).

---

## 2. How the miCAN-Stick2 works

> **Important — read this once.**

The miCAN-Stick2 is **not** a native SocketCAN adapter. According to the
miControl technical data, its *"virtual COM port protocol is an ASCII protocol
based on DS309-3"* (CiA 309-3, the CANopen ASCII gateway command set). In
practice:

- It plugs in as a **USB Virtual COM Port (VCP)** — on Linux it appears as
  `/dev/ttyUSB0` (via the in-kernel `ftdi_sio` driver) or `/dev/ttyACM0` (via
  `cdc_acm`). **No proprietary kernel module is required.**
- You control it by exchanging **ASCII text commands** ending in CR+LF.
- Received CAN messages are pushed back automatically as `MSG` lines.
- `candump` / `cansend` do **not** talk to it directly — use the `mican` tool
  or run the built-in [SocketCAN bridge](#8-socketcan-bridge).

---

## 3. Hardware: wiring, pinout, LEDs, terminator

### 3.1 CAN connector pinout

The miCAN-Stick2's CAN side is broken out on the connector as follows
(from the miControl *Klemmenbelegung*):

| Terminal | Signal    | miControl cable colour |
|----------|-----------|------------------------|
| X1.1     | reserved  | —                      |
| **X1.2** | **CAN Hi** (CAN High) | white      |
| **X1.3** | **CAN Lo** (CAN Low)  | green      |
| X1.4     | reserved  | —                      |
| **X1.5** | **CAN GND** (ground)  | black      |

### 3.2 Bus wiring rules (from the manual)

- Use cable to **ISO 11898**: twisted pair, shielded, **120 Ω** impedance.
- Connect **CAN High → CAN-Hi** and **CAN Low → CAN-Lo** on every node. Keep the
  colour assignment consistent (e.g. yellow = High, green = Low) to avoid
  mistakes.
- The whole bus must be terminated with **120 Ω at *both* ends** — exactly two
  terminators, no more.
- **Stub (drop) lines must be ≤ 2 cm.** Never connect more than two CAN cables
  at one connector (that would illegally branch the bus).
- For galvanically isolated devices, connect **CAN-GND to the reference
  potential exactly once** on the whole bus (either via the supply ground *or*
  via CAN-GND of a non-isolated device — not both).
- Bond the cable **shield once**, near the master, low-impedance to earth.

```
  miCAN-Stick2                          Drive / node (e.g. mcDSA-E60)
  X1.2 CAN Hi  ───────── CAN High ───────────  CAN-Hi
  X1.3 CAN Lo  ───────── CAN Low  ───────────  CAN-Lo
  X1.5 CAN GND ───────── CAN GND  ───────────  CAN-GND
       [120 Ω]  <- terminator on         terminator ->  [120 Ω]
       (RT switch = On)                  (at the far end of the bus)
```

### 3.3 Bus terminator switch (RT)

The stick has a built-in **120 Ω terminator** you switch with the **RT switch**:

| RT switch | Effect |
|-----------|--------|
| **On**    | 120 Ω terminator active — use this if the stick is at one **end** of the bus. |
| **Off**   | Terminator disconnected — use this if the stick is in the **middle** of the bus. |

Rule of thumb: the two devices at the two physical ends of the bus have their
terminators **On**; everything in between is **Off**.

### 3.4 Status LEDs

| LED | Colour | Normal operation | Meaning of other states |
|-----|--------|------------------|-------------------------|
| **LED0 Power** | green  | **on** | off = no supply · blinking = bootloader (no firmware) |
| **LED1 State** | yellow | **off** | on = bootloader mode |
| **LED2 Error** | red    | **off** | on = error |
| **LED3 Rx**    | green  | blinks on incoming message | off = nothing received |
| **LED4 Tx**    | yellow | blinks on outgoing message | off = nothing sent |

Healthy idle stick: **Power on, everything else off.**

---

## 4. Monkey-proof installation

You need: a Linux PC, the miCAN-Stick2, and a few minutes. Copy-paste the blocks
in order. You do **not** need to understand any of it.

### Step 0 — Get the code

```bash
git clone https://github.com/awk-dooh/mican-exwall-4linux.git
cd mican-exwall-4linux
```

### Step 1 — Run the installer

```bash
./scripts/install.sh
```

That script does everything for you:

- checks Python is present,
- creates an isolated environment in `./.venv`,
- installs the driver + dependencies,
- adds you to the `dialout` group (so you may open the serial port),
- installs a udev rule so the stick always shows up as **`/dev/mican0`**,
- runs a quick self-test.

> If it says you were added to the `dialout` group, **log out and back in**
> (or reboot) once. This is a one-time thing.

If you don't have Python yet, install it first:

```bash
sudo apt update && sudo apt install -y python3 python3-venv git   # Debian/Ubuntu
```

### Step 2 — Plug in the stick and activate the environment

```bash
source .venv/bin/activate
```

### Step 3 — Check it works

```bash
mican detect        # lists serial ports; your stick should be top of the list
mican version       # asks the stick who it is
```

If `mican version` prints a line like
`0x0139 0x15010001 1.92.03.00 0 128 1.10 0.0`, **you're done.** 🎉

> **Manual install (if you prefer not to use the script):**
> ```bash
> python3 -m venv .venv && source .venv/bin/activate
> pip install -e ".[bridge]"
> sudo usermod -aG dialout $USER      # then log out/in
> ```

---

## 5. First contact: talk to the stick

```bash
source .venv/bin/activate

# What port did it get?
mican detect

# Configure 500 kbit/s, go operational, and watch traffic:
mican monitor --bitrate 500000
```

`--port` is optional — the tool auto-detects `/dev/mican0` (or the single USB
serial port present). Pass `--port /dev/ttyUSB0` to be explicit.

---

## 6. Command-line usage

```bash
# List candidate serial ports (ranked best-match first)
mican detect

# Firmware / version info
mican version

# Configure bitrate, go operational, print every received frame
mican monitor --bitrate 500000

# Send one CAN frame (standard 11-bit id)
mican send --id 0x201 --data 1122 --bitrate 500000

# Send an extended (29-bit) frame
mican send --id 0x18FF50E5 --data 0011223344556677 --extended --bitrate 500000

# Bridge the stick to a virtual SocketCAN interface (see section 8)
mican bridge --can vcan0 --bitrate 500000
```

Supported bitrates (bit/s): **1000000, 800000, 500000, 250000, 125000, 100000,
50000, 20000** (the device expresses these in kbit/s internally).

Add `-v` or `-vv` for more logging.

### Totem device shortcuts

For the Exwall totem there are two convenience subcommands (see
[MICONTROL_4_DUMMIES.md](MICONTROL_4_DUMMIES.md)):

```bash
# mcIO-K1 (Node 4): switch the Edge-Backlight relay and show all I/O
mican io --backlight on   --bitrate 1000000
mican io --backlight off  --bitrate 1000000
mican io --set 0x0F       --bitrate 1000000    # drive Dout0..3 from a bitmask

# mcDSA-E60 (Node 1): run/stop the cooling fan and read status + temp sensor
mican fan --start --speed 30000 --bitrate 1000000
mican fan --stop  --bitrate 1000000
mican fan --reset-fault --bitrate 1000000
```

A full worked supervisor is in
[examples/keep_totem_happy.py](examples/keep_totem_happy.py)
(run with `--dry-run` first to observe without switching anything).

### Run the supervisor as a service (auto-start on boot)

To have the totem supervisor start automatically and restart on failure, install
it as a systemd service (after `scripts/install.sh` has created the venv):

```bash
sudo bash scripts/install_service.sh                 # uses /dev/mican0
sudo bash scripts/install_service.sh --port /dev/ttyUSB0

# manage it
systemctl status mican-totem.service
journalctl -u mican-totem.service -f                 # live logs
sudo systemctl restart mican-totem.service
sudo bash scripts/install_service.sh --uninstall
```

The installer fills the real paths/user into
[systemd/mican-totem.service.in](systemd/mican-totem.service.in), enables the
unit, and starts it. The driver's serial transport self-heals, and systemd
restarts the process if it ever exits.

---

## 7. Use it as a Python library

```python
from mican_stick2 import MiCanStick2, CanFrame, find_stick_port

port = find_stick_port(required=True)          # e.g. "/dev/mican0"

with MiCanStick2(port=port) as stick:
    print(stick.version_info())                # dict of version fields

    stick.set_bitrate(500_000)                 # kbit/s handled internally
    stick.start(node=0)                        # NMT start, 0 = broadcast

    # Send a raw CAN frame
    stick.send_frame(CanFrame(can_id=0x201, data=b"\x11\x22"))

    # CANopen SDO read/write (node 5)
    value = stick.read(0x3020, 0, "xi32", node=5)
    stick.write(0x3300, 0, "i32", -1000, node=5)

    # Receive frames for 5 seconds
    for frame in stick.receive_frames(timeout=5):
        print(frame)                           # e.g. "STD 0x185 [8] 50 00 ..."
```

The client is **thread-safe** and **auto-reconnects** if the USB device is
unplugged and replugged.

---

## 8. SocketCAN bridge

Expose the stick as a standard Linux CAN interface so `candump`, `cansend` and
`python-can` work against it.

```bash
# 1. Create a virtual CAN interface (one-time per boot)
sudo ./scripts/setup_vcan.sh vcan0

# 2. Start the bridge (leave it running)
source .venv/bin/activate
mican bridge --can vcan0 --bitrate 500000

# 3. In another terminal, use normal CAN tools:
candump vcan0
cansend vcan0 201#1122
```

The bridge forwards received CAN frames onto `vcan0`, and transmits frames
written to `vcan0` on the real bus. It needs `python-can` (installed by
`install.sh`).

---

## 9. Troubleshooting

**`mican detect` shows nothing / no `/dev/ttyUSB*`**
- Check `dmesg -w` while plugging in. You should see `ftdi_sio` or `cdc_acm`
  claim the device.
- Confirm the Power LED (green) is on.

**`Permission denied` opening the port**
- You're not in the `dialout` group yet, or you didn't log out/in after the
  installer added you. Run `groups` and check for `dialout`.

**`mican version` times out**
- Wrong port: pass `--port` explicitly.
- The stick is in bootloader mode (Power LED blinking / State LED on) — replug.

**Nothing received on the bus (`-582 TxTimeout` / `-583 ResponseTimeout`)**
- Bitrate mismatch — every node must use the **same** bitrate.
- Missing/!wrong **120 Ω termination** — check the RT switch and the far end.
- CAN-Hi/CAN-Lo swapped, or CAN-GND not connected. See [section 3](#3-hardware-wiring-pinout-leds-terminator).

**Exact USB VID:PID (for a perfect udev match)**
- Run `lsusb`, find your stick, note the `ID xxxx:yyyy`.
- Set it for auto-detection: `export MICAN_STICK2_VIDPID=0403:6001`
- Or edit `/etc/udev/rules.d/99-mican-stick2.rules` with your real IDs and run
  `sudo udevadm control --reload-rules && sudo udevadm trigger`.

### Device error codes

| Code | Meaning |
|------|---------|
| 100  | command not supported |
| 101  | syntax error (command could not be interpreted) |
| -541 | SdoWriteError (object could not be written) |
| -542 | SdoReadError (object could not be read) |
| -571 | BadCommand (value out of range) |
| -582 | TxTimeout (baudrate / terminator / bus wiring) |
| -583 | ResponseTimeout (target device off or not answering) |

---

## 10. Protocol reference

Confirmed from *AN1301 miControl Text-Kommandos*.

**Request:** `["seq"] [[net] node] command <args>` — terminated with `CR LF`.
Numbers may be decimal or hex (`0x` prefix). `net` is currently ignored; `node`
is the CANopen NodeId (`0` = broadcast for NMT commands).

**Response:** `["seq"] OK` · `["seq"] Error <code>` · `["seq"] <data>`.

**Notification (unsolicited):** `[net] MSG <cob_id> <len> 0xXX ...`
(extended frame if bit 29 of `cob_id` is set, i.e. `cob_id & 0x20000000`).

| Command | Purpose |
|---------|---------|
| `info version` | version string: `vendor product fw serial class proto impl` |
| `[net] init <kbit>` | set bitrate (`1000,800,500,250,125,100,50,20`) and **activate** the bus (`-1` = off) |
| `[[net] node] start\|stop\|preop\|reset node\|reset comm` | NMT state (node `0` = broadcast) |
| `[[net] node] r <idx> <sub> <type>` | CANopen SDO read |
| `[[net] node] w <idx> <sub> <type> <val>` | CANopen SDO write |
| `[net] set sdo_timeout <ms>` | SDO timeout (default 200 ms) |
| `[net] w m <cob_id> <len> <b0..b7>` | send raw CAN frame |
| `[net] r m <cob_id> [len]` | send remote frame (RTR) |

Data types: `i8 i16 i32 u8 u16 u32`; hex-output read variants `xi8 … xu32`.

The command formatting is centralised in the small `_addr` / `send_frame`
helpers of [`mican_stick2/client.py`](mican_stick2/client.py) and covered by the
offline tests in [`tests/test_protocol.py`](tests/test_protocol.py).

---

## 11. Project layout

```
mican_stick2/
  __init__.py     public API
  transport.py    self-healing serial transport (reconnect, locking, timeouts)
  client.py       DS309-3 ASCII client + CanFrame model
  discovery.py    USB port auto-detection (prefers /dev/mican0)
  bridge.py       optional SocketCAN (vcan) bridge via python-can
  cli.py          command-line interface (the `mican` command)
  __main__.py     `python -m mican_stick2`
scripts/
  install.sh      monkey-proof installer (venv, deps, dialout, udev, self-test)
  install_service.sh  install the totem supervisor as a systemd service
  setup_vcan.sh   create/bring up a vcan interface for the bridge
systemd/
  mican-totem.service.in   service template (filled in by install_service.sh)
examples/
  keep_totem_happy.py      worked totem supervisor (backlight + fan + sensor)
tests/
  test_protocol.py  offline protocol tests (no hardware needed)
  test_cli.py       offline tests for the io/fan CLI commands
pyproject.toml    packaging + the `mican` console script
requirements.txt  runtime dependencies
```

Run the tests any time with:

```bash
source .venv/bin/activate
pytest -q
```

---

*Not affiliated with miControl GmbH. "miCAN-Stick2" and "miControl" are
trademarks of their respective owner. Verify all wiring against the official
miControl documentation before energising equipment.*
