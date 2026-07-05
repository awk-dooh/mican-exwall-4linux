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

---

## Node 1 — mcDSA-E60 manufacturer-specific commissioning (0x3000 profile)

Beyond the standard DS402 objects, the mcDSA exposes miControl's native
parameter set for **motor setup, current limits, PWM and control-loop tuning**.
These were extracted from the decompiled `mcManual`
(*content_mcDSA-Exx_Parameter 3000h*).

> **Change motor/PWM parameters only while the output stage is disabled**
> (`disable_drive` / `enable_output(False)`), then persist them with
> `store_parameters` (which takes a few seconds).

### Objects

| Object | Sub | Type | Name | Meaning / range |
|--------|-----|------|------|-----------------|
| `0x3000` | 0 | u8  | DEV_Cmd | device command (see below) |
| `0x3004` | 0 | u8  | DEV_Enable | native output enable `{0,1}` |
| `0x3210` | 0 | i32 | CURR_Kp | current controller P |
| `0x3211` | 0 | i32 | CURR_Ki | current controller I |
| `0x3221` | 0 | u32 | CURR_LimitMaxPos | current limit + (device max applies) |
| `0x3223` | 0 | u32 | CURR_LimitMaxNeg | current limit − |
| `0x3310` | 0 | i32 | VEL_Kp | vel/pos controller P |
| `0x3311` | 0 | i32 | VEL_Ki | vel/pos controller I |
| `0x3312` | 0 | i32 | VEL_Kd | vel/pos controller D |
| `0x3314` | 0 | u16 | VEL_Kvff | velocity feed-forward `[0..2000]` |
| `0x3350` | 0 | u32 | VEL_Feedback | feedback source (encoder/hall/EMK) |
| `0x3830` | 0 | u32 | PWM_Frequency | `{12500,25000,32000,50000,100000,200000}` Hz, default 25000 |
| `0x3900` | 0 | u8  | MOTOR_Type | `0`=DC, `1`=BLDC |
| `0x3901` | 0 | u16 | MOTOR_Nn | nominal speed `[0..30000]` rpm |
| `0x3902` | 0 | u16 | MOTOR_Un | nominal voltage `[0..60000]` |
| `0x3910` | 0 | u8  | MOTOR_PolN | pole count `[1..100]` |
| `0x3911` | 0 | u16 | MOTOR_Polarity | direction/polarity bitfield |
| `0x3962` | 0 | u32 | MOTOR_ENC_Resolution | encoder increments |
| `0x5000` | 0 | i16 | MPU_Cmd | on-device MPU program control |

### DEV_Cmd (`0x3000`) values

| Value | Name | Effect |
|-------|------|--------|
| `0x01` | ClearError | clear fault (re-enables if it was enabled) |
| `0x02` | QuickStop | stop on quick-stop ramp |
| `0x03` | Halt | stop on normal ramp |
| `0x04` | Continue | resume after halt/quick-stop |
| `0x05` | Update | apply new motion setpoints |
| `0x80` | StoreParam | **save** 0x3000-range params to EEPROM |
| `0x81` | RestoreParam | reload params from EEPROM |
| `0x82` | DefaultParam | reset params to defaults (RAM only) |
| `0x83` | ClearParam | default + store |

### Convenience helpers

```python
NODE = 1
stick.enable_output(False, node=NODE)             # disable before config

stick.configure_motor(
    motor_type=1,              # BLDC
    pole_count=8,
    nominal_speed=3000,        # rpm
    encoder_resolution=4096,
    node=NODE,
)
stick.set_pwm_frequency(25000, node=NODE)
stick.set_current_limits(5000, node=NODE)         # +/- (device max applies)
stick.set_current_gains(kp=200, ki=50, node=NODE)
stick.set_velocity_gains(kp=100, ki=20, kd=5, kvff=1000, node=NODE)

stick.store_parameters(node=NODE)                 # persist to EEPROM (~seconds)

# runtime motion control via the native command interface:
stick.enable_output(True, node=NODE)
stick.clear_error(node=NODE)
stick.quick_stop(node=NODE)
stick.halt(node=NODE)
stick.continue_motion(node=NODE)
```

Equivalent raw stick commands:

```
1 w 0x3004 0 u8 0        # disable output
1 w 0x3900 0 u8 1        # BLDC
1 w 0x3910 0 u8 8        # pole count
1 w 0x3830 0 u32 25000   # PWM 25 kHz
1 w 0x3221 0 u32 5000    # current limit +
1 w 0x3223 0 u32 5000    # current limit -
1 w 0x3310 0 i32 100     # VEL_Kp
1 w 0x3000 0 u8 128      # StoreParam (0x80)
```

> These parameters and ranges are transcribed from the decompiled manual and
> apply to the mcDSA-Exx family. The **absolute current maximum is
> device-specific** (mcDSA-E60) — see its datasheet before raising limits.
