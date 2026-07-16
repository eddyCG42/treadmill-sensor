# Bill of Materials (BOM)

## Core Electronics

| Component | Part Number | Source | Notes |
|---|---|---|---|
| Adafruit Feather nRF52840 Express | [#3406](https://www.adafruit.com/product/3406) | Adafruit | MCU on main sensor PCB, runs S340 SoftDevice. USB serial to Pi; BLE **speed + cadence + grade** to Fenix 8 |
| LSM303DLHC | — | JLCPCB (SMT) / DigiKey | Accel/magnetometer on main sensor PCB; the accelerometer also serves as the **cadence pickup** (footfall vibration through the frame) |
| VL53L4CD (Pololu carrier) | [Pololu #3692](https://www.pololu.com/product/3692) | Pololu | Time-of-flight distance sensor on main PCB — reads deck tilt for **incline / grade** |
| DRV5023 Hall-Effect Sensor | DRV5023AJQLPG | JLCPCB (SMT) | On separate 60 mm PCB, wired to main PCB |
| Raspberry Pi 3B+ or 4 | — | Various | Dashboard host, hostname `EddyPi` |
| HyperPixel Display | — | Pimoroni | SPI touchscreen for dashboard UI |
| Garmin HRM-Pro Plus | — | Garmin | BLE to Pi + ANT+ to Fenix 8 simultaneously |
| Garmin Fenix 8 | — | Garmin | Receives **speed + cadence + grade** (BLE from Feather) + HR (ANT+ from HRM-Pro Plus) |
| J-Link EDU Mini | — | SEGGER / DigiKey | For SoftDevice flashing & recovery |

## Custom PCBs (JLCPCB / EasyEDA Pro)

### Hall Sensor PCB

| Item | Details |
|---|---|
| Board size | 60 mm |
| Surface finish | ENIG |
| Assembly | Standard PCBA tier, SMT |
| Hall sensor | DRV5023 (bottom-side) |
| Connector | J1 screw terminal (included in assembly) |
| Status LED | NationStar NCD0603C1 (LCSC: C84264), 0603 yellow-green |
| LED note | Substituted for out-of-stock C72043; C84264 is Extended part (~$3 loading fee) |

### Main Sensor PCB (Feather + LSM303DLHC + VL53L4CD)

| Item | Details |
|---|---|
| MCU | Adafruit Feather nRF52840 (through-hole, created manually in EasyEDA Pro) |
| IMU | LSM303DLHC (created manually in EasyEDA Pro after library import issues) — also the cadence pickup |
| ToF sensor | VL53L4CD (Pololu carrier) over I²C — deck-tilt / incline sensing |
| Connection to Pi | USB serial |
| Connection to Hall PCB | Wired (screw terminal / header) |

## Firmware / Programming

| Item | Notes |
|---|---|
| SoftDevice | S340 (BLE + ANT+) — **not** S140 |
| Flash offset | Application starts at `0x31000` |
| Toolchain | Arduino IDE with Adafruit nRF52 BSP (modified for S340) |
| Debugger | J-Link EDU Mini (SWD) |

## Passive Components

> Exact values depend on your PCB revision — see schematic in `pcb/easyeda/`.

## Enclosures

> ⏳ **Planned — not yet published in this repo.** The OpenSCAD sources are still
> being cleaned up (see the Roadmap in the main README).

| File | Description | Method |
|---|---|---|
| `pi-hyperpixel-clamp.scad` | Pi + HyperPixel → treadmill arm clamp | 3D print (FDM) |
| `feather-sensor-mount.scad` | Feather sensor PCB mount (≤30 mm profile) | 3D print (FDM), strap/zip-tie |

## Software Dependencies (Pi)

The dashboard server is pure Python **standard library** (`http.server`); the
only third-party packages are in `dashboard/requirements.txt`:

- `pyserial` — USB serial link to the Feather
- `bleak` — BLE heart-rate strap
- `requests` — Strava API uploads
- Python 3.9+

## Where to Buy (Canada)

| Supplier | What |
|---|---|
| [Adafruit](https://www.adafruit.com/) | Feather, sensors, breakouts |
| [DigiKey Canada](https://www.digikey.ca/) | J-Link EDU Mini, passives |
| [JLCPCB](https://jlcpcb.com/) | PCB fab + SMT assembly |
| [Amazon.ca](https://www.amazon.ca/) | Raspberry Pi, HyperPixel, misc |
