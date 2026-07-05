# CANopen reference — the totem devices behind the miCAN-Stick2

This documents the two miControl CANopen nodes wired to the miCAN-Stick2 in the
Exwall / digital City-Light-Poster totem (schematic **DT.0618_Rev.03**), and how
to command them with this package.

## Bus topology

```
   NUC mini-PC ──USB──▶ miCAN-Stick2 ──CAN(Lo/Hi/GND)──┬──▶ mcDSA-E60  (Node-ID 1)  servo drive
                        (RT terminator OFF)            └──▶ mcIO-K1    (Node-ID 4)  digital I/O
```

All three share **one CAN segment** (nets `CAN-GND1 / Lo_1 / Hi_1` in the
drawing). Bus parameters: **CAN 2.0B, up to 1 Mbit/s.** The management software
runs on the **NUC**, not on a remote Windows PC.

| Node | Device | Article | Profile | Role |
|------|--------|---------|---------|------|
| **1** | mcDSA-E60 | 1511656 | DS301 + **DS402** | servo motor drive |
| **4** | mcIO-K1 | 1212161 | DS301 + **DS401** | 12 digital in / 4 digital out |

> Neither device is galvanically isolated on CAN (*"Galvanisch getrennt: nein"*).
> The stick **is** isolated. Keep CAN-GND common across all nodes.

---

## Connecting

```python
from mican_stick2 import MiCanStick2

stick = MiCanStick2(port="/dev/mican0")   # or let discovery find it
stick.open()
stick.set_bitrate(1_000_000)              # both nodes support up to 1 Mbit/s
stick.start(0)                            # NMT start (broadcast) -> Operational
```

Verify you are really talking to each node:

```python
print(stick.identity(node=1))   # {'vendor_id': 0x139, 'product_code': ...}
print(stick.device_type(node=4))
```

---

## Node 1 — mcDSA-E60 servo (DS402)

### Objects

| Object | Sub | Type | Access | Meaning |
|--------|-----|------|--------|---------|
| `0x6040` | 0 | u16 | RW | Controlword (state machine) |
| `0x6041` | 0 | u16 | RO | Statusword |
| `0x6060` | 0 | i8  | RW | Modes of operation |
| `0x6061` | 0 | i8  | RO | Modes of operation display |
| `0x6064` | 0 | i32 | RO | Position actual value |
| `0x606C` | 0 | i32 | RO | Velocity actual value |
| `0x607A` | 0 | i32 | RW | Target position |
| `0x6081` | 0 | u32 | RW | Profile velocity |
| `0x6083` | 0 | u32 | RW | Profile acceleration |
| `0x60FF` | 0 | i32 | RW | Target velocity |
| `0x6071` | 0 | i16 | RW | Target torque |

### Controlword state machine (`0x6040`)

| Command | Value | Result |
|---------|-------|--------|
| Fault reset | `0x80` | clears fault |
| Shutdown | `0x06` | Ready to switch on |
| Switch on | `0x07` | Switched on |
| Enable operation | `0x0F` | **Operation enabled** |
| New set-point (PP mode) | `0x1F` | latch target position (bit 4) |

### Statusword (`0x6041`) bits
bit0 Ready · bit1 Switched on · bit2 **Operation enabled** · bit3 **Fault** ·
bit10 Target reached.

### Convenience helpers

```python
NODE = 1
stick.reset_fault(node=NODE)                      # if a fault is latched
stick.enable_drive(node=NODE, mode=3)             # PP=1, PV=3, PT=4, Homing=6
stick.set_velocity(50_000, node=NODE)             # Profile Velocity mode

print(stick.read_statusword(node=NODE))           # int
print(stick.read_position(node=NODE))             # counts
print(stick.read_velocity(node=NODE))
print(stick.is_operation_enabled(node=NODE))      # bool

# Profile Position move (latches the new set-point for you):
stick.enable_drive(node=NODE, mode=1)
stick.set_target_position(100_000, node=NODE)     # absolute
stick.set_target_position(2_000, node=NODE, relative=True)

stick.disable_drive(node=NODE)                    # remove torque
```

Equivalent raw stick commands:

```
1 w 0x6040 0 u16 6      # shutdown
1 w 0x6040 0 u16 7      # switch on
1 w 0x6060 0 i8 3       # profile velocity mode
1 w 0x6040 0 u16 15     # enable operation
1 w 0x60FF 0 i32 50000  # target velocity
1 r 0x6041 0 xu16       # read statusword
```

---

## Node 4 — mcIO-K1 digital I/O (DS401)

### Objects

| Object | Sub | Type | Access | Meaning |
|--------|-----|------|--------|---------|
| `0x6000` | 1 | u8 | RO | Digital inputs Din0..7 |
| `0x6000` | 2 | u8 | RO | Digital inputs Din8..11 (bits 0..3) |
| `0x6200` | 1 | u8 | RW | Digital outputs Dout0..3 (bits 0..3) |

> On the mcIO-K1, **Din8..11 are the same terminals as Dout0..3** (parallel),
> so a driven output reads back on the matching input. In the schematic, the
> Wago relay `-8K1` (Edge Backlight) is on one of these outputs.

### Convenience helpers

```python
NODE = 4
stick.set_outputs(0x01, node=NODE)                # Dout0 ON (e.g. backlight)
stick.set_outputs(0x0F, node=NODE)                # all four outputs ON
print(stick.read_outputs(node=NODE))              # read back Dout0..3

low  = stick.read_inputs(node=NODE)               # Din0..7
high = stick.read_inputs(node=NODE, subindex=2)   # Din8..11
edge_backlight_on = bool(high & 0x01)
```

Equivalent raw stick commands:

```
4 w 0x6200 1 u8 0x01    # Dout0 ON
4 r 0x6000 1 xu8        # read Din0..7
4 r 0x6000 2 xu8        # read Din8..11
```

---

## Notes and caveats

- The **profiles** (DS402/DS401) fix the objects above. **Manufacturer-specific**
  objects (0x2000–0x5FFF: motor commutation/current limits/PWM frequency, input
  polarity, etc.) live in the miControl `mcManual` and are not covered here.
- The drive must be walked through the DS402 state machine (`enable_drive`)
  before it produces torque; a latched **Fault** (statusword bit 3) must be
  cleared with `reset_fault` first.
- All commands go through the single serial connection to the stick; address the
  target with the `node=` argument (1 = servo, 4 = I/O).
