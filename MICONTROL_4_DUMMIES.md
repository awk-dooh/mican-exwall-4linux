# MiControl for Dummies

*A plain-language guide to the miControl devices in the Exwall totem — what they
are, how they're wired, what they do, and how to talk to them from the Linux box.*

Based on the totem wiring diagram **"Schaltplan" DT.0618_Rev.03**
(dCLP 85″ Samsung). No prior CAN/CANopen knowledge assumed.

---

## 1. The big picture in one minute

The totem is a big outdoor advertising screen (an 85″ Samsung display). Around
that screen there is some **electronics that keeps it alive**: cooling fans, a
backlight for the poster frame, temperature and status sensors.

A little **NUC mini-PC** is the brain. But a PC can't directly switch a relay or
spin a fan — it only has USB. So miControl provides small boxes that *can* do
electrical things, and they all talk to each other over a shared two-wire bus
called **CAN**.

Think of it like this:

| Real-world analogy | Device | Job |
|--------------------|--------|-----|
| The **brain** | NUC mini-PC | runs your software (this Python package) |
| The **translator** | miCAN-Stick2 | turns USB ⇄ CAN so the PC can join the bus |
| The **muscle** | mcDSA-E60 | drives the cooling fan motor, reads temperature |
| The **switchboard** | mcIO-K1 | switches things on/off (backlight), reads status |

They are wired in a chain and each has a number (a **Node-ID**) so messages know
where to go.

---

## 2. How they're wired (from the Schaltplan)

```
   ┌───────────────┐
   │  NUC mini-PC  │   (page /9 of the plan)
   └──────┬────────┘
          │ USB cable
   ┌──────▼────────┐
   │ miCAN-Stick2  │   USB→CAN translator (page /9, tag -9U1)
   │ (RT switch    │   ← its terminator resistor is switched OFF
   │  = OFF)       │
   └──────┬────────┘
          │ 3 wires:  CAN-Hi, CAN-Lo, CAN-GND
          │ (one shared "party line")
   ┌──────┴───────────────────────────┐
   │                                   │
┌──▼───────────────┐          ┌────────▼─────────┐
│  mcDSA-E60        │          │  mcIO-K1         │
│  Node-ID = 1      │          │  Node-ID = 4     │
│  (page /5)        │          │  (page /8)       │
│  the fan drive    │          │  the I/O box     │
└──┬────────────────┘          └────────┬─────────┘
   │ motor phases + sensors             │ relays + digital in/out
   ▼                                    ▼
 cooling fan(s),                    Edge-Backlight relay (-8K1),
 temperature sensor,               status inputs (e.g. overvoltage)
 hall/tacho feedback
```

**Key facts (all from the drawing):**

- It is a **single CAN bus**. All three devices share the same three wires
  (CAN-Hi / CAN-Lo / CAN-GND). This is why they can all talk to each other.
- Each device has a **number**: the fan drive is **Node 1**, the I/O box is
  **Node 4**. You use these numbers in software to say "who I'm talking to."
- The stick's **terminator switch (RT) is OFF** in this build — the 120 Ω
  end-of-line resistors live at the other ends of the bus. (If communication is
  flaky on the bench, this is the first thing to check.)
- Everything runs at up to **1 Mbit/s**.

> A CAN bus looks a lot like RS-485 (two wires, differential). The difference is
> that CAN also has a whole *language* built in (addresses, acknowledgements,
> error checking). RS-485 is just the wire; CAN is the wire **plus** the postal
> system.

---

## 3. What each device actually does

### The miCAN-Stick2 — the translator
Plugs into the NUC's USB and shows up on Linux as a serial port
(`/dev/ttyUSB0`, or `/dev/mican0` after install). It doesn't "do" anything on
its own — it just carries your messages onto the CAN bus and brings replies
back. All the clever stuff is in the Python software on the NUC.

### The mcDSA-E60 — the muscle (Node 1)
A small **servo/motor drive**. In this totem it lives on the *"FAN's control
board"* and drives the **cooling fan** for the display, using:
- **motor phases** (Ma/Mb/Mc) to actually turn the fan,
- **hall sensors / tacho** to know how fast it's spinning,
- an **analog input (ain0)** wired to a **10 kΩ temperature sensor (NTC)** so the
  system knows how hot it's getting.

You tell it "spin at this speed" or "stop", and it does the electrical work.

### The mcIO-K1 — the switchboard (Node 4)
A **digital input/output box**: 12 inputs and 4 outputs. In this totem:
- one of its **outputs drives the "Edge Backlight" relay** (the Wago relay
  `-8K1`, on output **Dout3**) — that's how the poster-frame lighting is switched,
- its **inputs read status signals** (for example the surge-protector /
  over-voltage status).

You tell it "turn output 3 on" or ask it "what do the inputs read?"

---

## 4. Talking to them from Linux

First install the package (see the main [README](README.md) — `bash
scripts/install.sh` does everything). Then open a Python shell on the NUC.

Every example below is **runnable**. Node **1** = fan drive, Node **4** = I/O box.

### 4.1 Say hello — find the stick and check the devices

```python
from mican_stick2 import MiCanStick2

stick = MiCanStick2(port="/dev/mican0")   # auto-found after install
stick.open()
stick.set_bitrate(1_000_000)              # 1 Mbit/s, like the totem
stick.start(0)                            # wake all nodes (NMT start, broadcast)

print("Fan drive :", stick.identity(node=1))   # should report vendor 0x139
print("I/O box   :", stick.identity(node=4))
```

If those print without errors, you are on the bus and both devices answer. 🎉

### 4.2 Switch the Edge Backlight on and off (mcIO-K1, Node 4)

The Edge-Backlight relay is on **Dout3** = bit 3 = value `0x08`.

```python
IO = 4

stick.set_outputs(0x08, node=IO)   # backlight ON  (Dout3 high)
# ... wait, look at the totem ...
stick.set_outputs(0x00, node=IO)   # backlight OFF (all outputs low)
```

Want to control several outputs at once? Each output is one bit:
`Dout0=0x01, Dout1=0x02, Dout2=0x04, Dout3=0x08`. So `0x09` = Dout0 + Dout3 on.

### 4.3 Read the status inputs (mcIO-K1, Node 4)

```python
IO = 4

inputs = stick.read_inputs(node=IO)          # Din0..7 as one byte
print(f"inputs = 0b{inputs:08b}")

overvoltage_ok = bool(inputs & 0x01)         # Din0 in this totem = overvoltage status
print("Surge protector says:", "OK" if overvoltage_ok else "TRIPPED")
```

(Din8..11 — which are shared with the outputs — are read with
`stick.read_inputs(node=IO, subindex=2)`.)

### 4.4 Run the cooling fan (mcDSA-E60, Node 1)

The drive uses a small "get ready" sequence before it will turn (this is
standard for motor drives — it's a safety interlock).

```python
FAN = 1

stick.enable_drive(node=FAN, mode=3)   # mode 3 = "spin at a set speed"
stick.set_velocity(30_000, node=FAN)   # start turning (units are drive counts)

print("statusword:", hex(stick.read_statusword(node=FAN)))
print("running?  :", stick.is_operation_enabled(node=FAN))

# ... later ...
stick.set_velocity(0, node=FAN)        # slow to a stop
stick.disable_drive(node=FAN)          # remove power from the motor
```

If it refuses to move, clear any latched fault first:

```python
stick.reset_fault(node=FAN)
```

### 4.5 Read the temperature sensor (mcDSA-E60, Node 1)

The 10 kΩ NTC temperature sensor is wired to the drive's **analog input 0**
(`IO_AIN0`, object `0x3100`). The drive reports the voltage in **millivolts**:

```python
FAN = 1

millivolts = stick.read_analog_input(0, node=FAN)   # e.g. 2345 = 2.345 V
print(f"sensor = {millivolts} mV")
```

That's the *raw voltage*. To turn it into °C you apply the NTC's conversion
curve (from the sensor's datasheet). A simple placeholder:

```python
def mv_to_celsius(mv):
    # TODO: replace with the real NTC curve for the fitted sensor
    return (mv - 500) / 20.0

print("approx temp:", mv_to_celsius(millivolts), "°C")
```

### 4.6 A tiny "keep the totem happy" loop

This ties it together: if the surge protector is OK, make sure the backlight is
on; otherwise switch it off. It also speeds the fan up when the sensor voltage
rises (hotter):

```python
import time
from mican_stick2 import MiCanStick2

FAN, IO = 1, 4

with MiCanStick2(port="/dev/mican0") as stick:
    stick.set_bitrate(1_000_000)
    stick.start(0)
    stick.enable_drive(node=FAN, mode=3)      # ready the fan

    backlight_on = False
    while True:
        inputs = stick.read_inputs(node=IO)
        power_ok = bool(inputs & 0x01)         # Din0 = overvoltage status

        if power_ok and not backlight_on:
            stick.set_outputs(0x08, node=IO)   # backlight ON
            backlight_on = True
            print("Backlight ON")
        elif not power_ok and backlight_on:
            stick.set_outputs(0x00, node=IO)   # backlight OFF
            backlight_on = False
            print("Power problem — backlight OFF")

        # crude temperature-driven fan: hotter sensor -> faster fan
        mv = stick.read_analog_input(0, node=FAN)
        speed = max(10_000, min(40_000, mv * 10))
        stick.set_velocity(speed, node=FAN)

        time.sleep(2)
```

Run it with `python3 keep_totem_happy.py`. Press Ctrl-C to stop.

---

## 5. "Which number is which?" cheat-sheet

| I want to… | Device | Node | Call |
|------------|--------|------|------|
| turn the edge backlight on/off | mcIO-K1 | 4 | `set_outputs(0x08 / 0x00, node=4)` |
| read a status input | mcIO-K1 | 4 | `read_inputs(node=4)` |
| start / stop the fan | mcDSA-E60 | 1 | `enable_drive(node=1)` / `set_velocity(v, node=1)` |
| check the fan is running | mcDSA-E60 | 1 | `is_operation_enabled(node=1)` |
| read the temperature sensor | mcDSA-E60 | 1 | `read_analog_input(0, node=1)` (mV) |
| clear a drive fault | mcDSA-E60 | 1 | `reset_fault(node=1)` |
| identify a device | either | 1 or 4 | `identity(node=…)` |

---

## 6. Safety & gotchas (please read once)

- **The fan is a real motor.** `enable_drive` + `set_velocity` will make it
  spin. Keep fingers/cables clear and start with a low speed.
- **Outputs switch real loads** (the backlight relay carries mains-side power via
  the Wago relay). Only toggle them when you know what's connected.
- **Confirm the exact pin/bit mapping against the Schaltplan for your specific
  unit.** The Node-IDs (1 and 4) and "backlight on Dout3 / overvoltage on Din0"
  come from drawing DT.0618_Rev.03; a different revision may differ.
- **Terminator:** if you get `TxTimeout` errors, check bus termination (120 Ω at
  both ends) and that `set_bitrate(1_000_000)` matches the devices.
- **The software runs on the NUC**, the machine physically wired to the stick —
  not on a remote Windows laptop.

---

## 7. Where to go next

- [README.md](README.md) — install, wiring, LEDs, command-line tool.
- [CANOPEN.md](CANOPEN.md) — the full object list and every helper, including
  motor tuning (PWM frequency, current limits, control-loop gains) for the
  mcDSA-E60.
- [ARCHITECTURE.md](ARCHITECTURE.md) — how the software itself is built.

You now know enough to switch a light, read a sensor and spin a fan on the totem
from Python. That's 90 % of what this system does.
