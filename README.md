# 🏃 Treadmill Sensor

> DIY speed, **cadence** & heart-rate dashboard for the NordicTrack Commercial 2450 — feeds a Garmin Fenix 8 with speed, cadence and grade over BLE, adds live BLE heart rate, ToF-based incline sensing, auto-pause, virtual GPS routes and one-tap Strava upload.

<!-- Badges — update eddyCG42 after pushing -->
![License](https://img.shields.io/badge/license-GPL%20v3-blue)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-C51A4A?logo=raspberrypi)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![Firmware](https://img.shields.io/badge/firmware-nRF52840%20%2B%20S340-00A9CE)
![PCB](https://img.shields.io/badge/PCB-JLCPCB-green)

---

![Run screen](docs/images/cockpit.png)

<sub>The in-run cockpit on the 800×480 kiosk — distance, time, HR zone, pace, live cadence and grade. (Rendered mock-up of the actual UI.)</sub>

## ✨ Features

| Feature | Details |
|---|---|
| **Live Speed Tracking** | DRV5023 Hall-effect sensor (separate PCB) → Feather nRF52840 → Pi via USB serial |
| **Real Cadence** | Footfall cadence detected on the Pi from the frame accelerometer (band-pass + windowed autocorrelation), no foot pod — validated ±4 spm from walk to run, pushed to the Fenix over BLE |
| **Incline / Grade** | VL53L4CD time-of-flight sensor reads deck tilt; 6-point calibration maps it to % grade, mirrored to the Fenix so watch grade matches the dashboard |
| **Heart Rate** | Garmin HRM-Pro Plus chest strap → Pi via BLE (`bleak`) + Fenix 8 via ANT+ simultaneously; Karvonen HR zones |
| **Garmin Fenix 8 Integration** | Feather broadcasts **speed + cadence + grade** over BLE to the Fenix 8; HRM-Pro Plus sends HR over ANT+ |
| **Web Dashboard** | Python HTTP server + React/JSX kiosk UI on a HyperPixel touchscreen, mounted on the treadmill arm |
| **Virtual Routes** | Fixed 400 m athletics track or a random pick from your GPX pool (anti-repeat) for nicer Strava maps |
| **Auto-Pause** | Detects belt stop → yellow "AUTO-PAUSE" state |
| **Strava Upload** | One-tap SAVE → export `.tcx`/`.fit` and auto-upload via the Strava API, with an offline retry queue |
| **Custom PCBs** | Hall sensor PCB (60 mm, JLCPCB) + main sensor PCB (Feather + LSM303DLHC + VL53L4CD) |
| **3D-Printable Enclosures** | OpenSCAD: Pi + HyperPixel arm clamp & Feather sensor mount |

---

## 📐 Architecture

```
┌──────────────┐  wire   ┌──────────────────┐  USB     ┌──────────────────┐  SPI    ┌────────────┐
│  DRV5023     │ ──────► │  Feather         │ serial   │  Raspberry Pi    │ ──────► │ HyperPixel │
│  Hall Sensor │         │  nRF52840        │ ───────► │  (EddyPi)        │         │  Display   │
│  (separate   │         │  +LSM303 +ToF    │         │                  │         └────────────┘
│   PCB)       │         │  (main PCB)      │         │  Web Dashboard   │
└──────────────┘         └──────┬───────────┘         │  HR monitor      │  HTTPS
                                │ BLE                  │  Strava uploader │ ──────► Strava API
                                ▼                      │                  │
                         ┌──────────────┐              │                  │
                         │  Garmin      │              │                  │
                         │  Fenix 8     │◄── ANT+ ────│◄── BLE ─────────│
                         └──────────────┘              └──────────────────┘
                                ▲                              ▲
                           ANT+ │                         BLE  │
                         ┌──────┴───────┐                      │
                         │  Garmin      │──────────────────────┘
                         │  HRM-Pro Plus│
                         └──────────────┘
```

**Over BLE** the Feather sends the Fenix 8 **speed, cadence and grade**. The Pi
computes cadence (from the frame accelerometer) and grade (from the ToF sensor)
and pushes them to the Feather, which relays them to the watch; the Feather keeps
its own onboard estimates as a fallback if the Pi push goes stale.

---

## 🧰 Hardware

See the full **[Bill of Materials](docs/BOM.md)** for part numbers and sourcing.

### Core Components

- **Raspberry Pi** (3B+ or 4) — dashboard host (`EddyPi`), receives speed data via USB serial, HR via BLE
- **Adafruit Feather nRF52840** — sensor MCU on main PCB, running S340 SoftDevice (BLE + ANT+), communicates with Pi over USB serial and broadcasts speed to Garmin Fenix 8 over BLE
- **LSM303DLHC** — accelerometer/magnetometer on the main PCB; the accelerometer doubles as the **cadence pickup** (footfall vibration through the frame)
- **VL53L4CD** — time-of-flight distance sensor on the main PCB, reads deck tilt for **incline / grade** (6-point calibration → % grade)
- **DRV5023 Hall-Effect Sensor** — on a separate 60 mm PCB, wired to the Feather, detects belt magnets
- **HyperPixel Display** — SPI touchscreen dashboard, mounted on treadmill arm
- **Garmin HRM-Pro Plus** — sends HR to Pi via BLE and to Garmin Fenix 8 via ANT+ simultaneously
- **Garmin Fenix 8** — receives speed from Feather (BLE) and HR from HRM-Pro Plus (ANT+)

### Custom PCBs

**Hall Sensor PCB** (60 mm) — designed in **EasyEDA Pro**, manufactured by JLCPCB:
- ENIG surface finish
- SMT assembly (DRV5023 bottom-side, Standard PCBA tier)
- J1 screw terminal for wiring to main PCB
- Status LED: NationStar NCD0603C1 (0603, yellow-green)

**Main Sensor PCB** — carries the Feather nRF52840 + LSM303DLHC + VL53L4CD:
- Connects to Hall sensor PCB via wiring
- USB connection to Raspberry Pi for serial data
- Designed in EasyEDA Pro

<!-- TODO: Add PCB render or photo -->
<!-- ![PCB Front](docs/images/pcb-front.jpg) -->

### Enclosures (OpenSCAD)

| Enclosure | Description |
|---|---|
| `pi-hyperpixel-clamp.scad` | Clamps Pi + HyperPixel to treadmill arm |
| `feather-sensor-mount.scad` | 30 mm max profile, strap/zip-tie mount for sensor PCB |

---

## 🚀 Getting Started

### Prerequisites

- Raspberry Pi running Raspberry Pi OS (Bookworm or later)
- Python 3.9+
- Feather nRF52840 with S340 SoftDevice flashed
- Strava API application (for upload feature)

### 1. Clone the Repo

```bash
git clone https://github.com/eddyCG42/treadmill-sensor.git
cd treadmill-sensor
```

### 2. Install Dependencies

```bash
pip install -r dashboard/requirements.txt
```

### 3. Configure Strava (optional)

Create `~/strava_config.json` on your Pi:

```json
{
  "client_id": "YOUR_CLIENT_ID",
  "client_secret": "YOUR_CLIENT_SECRET"
}
```

> ⚠️ **Never commit your Strava tokens.** They are excluded via `.gitignore`.

### 4. Pair the HRM-Pro Plus

The dashboard connects via BLE using `bleak`. Set your strap's MAC as `hr_mac`
in `~/treadmill_config.json` (with `hr_max` / `hr_rest` for the Karvonen zones).

### 5. Install & Run (dashboard + kiosk)

```bash
cd dashboard/web_dashboard
bash deploy.sh   # installs the systemd services, fetches vendored React/Babel, starts everything
```

`deploy.sh` generates and enables the `treadmill-server` (system) and
`treadmill-kiosk` (user) units. See [`dashboard/DEPLOY_v12.md`](dashboard/DEPLOY_v12.md)
for the full coupled firmware + Pi deployment guide.

### 6. Flash the Feather Firmware

See [Firmware Notes](#-firmware-notes) below and the [Adafruit nRF52 Bootloader S340 PR](https://github.com/adafruit/Adafruit_nRF52_Bootloader/pull/359) for Arduino IDE build instructions and S340 SoftDevice setup.

---

## 📁 Project Structure

```
treadmill-sensor/
├── README.md
├── LICENSE
├── .gitignore / .gitattributes
├── docs/
│   └── BOM.md                       ← Bill of materials
├── firmware/
│   └── feather-nrf52840/
│       └── treadmill_sensor_v11_21.ino   ← Arduino sketch (internal v12.1)
├── dashboard/
│   ├── treadmill_cadence.py         ← Pi-side footfall / cadence detector
│   ├── treadmill_metrics.py         ← shared speed / incline / elevation math
│   ├── treadmill_routes.py          ← GPX virtual routes (random + fixed track)
│   ├── treadmill_export.py          ← TCX / FIT activity export
│   ├── treadmill_hr.py              ← BLE heart-rate monitor
│   ├── treadmill_strava.py          ← Strava OAuth + upload
│   ├── treadmill_strava_queue.py    ← offline upload queue
│   ├── cadence_calibrate.py         ← offline cadence-calibration tool
│   ├── requirements.txt
│   ├── DEPLOY_v12.md / RECORD_CADENCE.md
│   └── web_dashboard/
│       ├── treadmill_server.py      ← HTTP server + run state machine
│       ├── deploy.sh / setup.sh
│       ├── static/                  ← dashboard.html + *.jsx (React, in-browser Babel)
│       └── kiosk/                   ← systemd units + Chromium kiosk launcher
├── pcb/
│   └── easyeda/                     ← Gerber exports, schematic PDF
└── strava_config.json.example       ← Template (no secrets)
```

---

## 📊 Dashboard

| Home / mode select | Cadence calibration |
|---|---|
| ![Home screen](docs/images/home.png) | ![Cadence calibration](docs/images/cadence-calibration.png) |

<sub>Left: pick the fixed 400 m track or a random GPX route, with a live sensor
check before starting. Right: the CADENCE tab — the "VERROUILLAGE DÉTECTEUR" bar
shows the Pi footfall detector locking onto your rhythm (green = locked).</sub>

The web dashboard (`dashboard/web_dashboard/treadmill_server.py`, a lightweight
Python HTTP server + React/JSX kiosk UI) provides:

- **Live speed** from the Hall-effect sensor (via Feather → USB serial)
- **Real cadence** — band-pass + windowed-autocorrelation footfall detection on
  the Pi from the frame accelerometer, pushed to Garmin over BLE (validated ±4 spm)
- **Heart rate** from HRM-Pro Plus (BLE via `bleak`) with Karvonen HR zones
- **Incline** — 6-point ToF calibration, mirrored to Garmin so grade matches
- **Distance & duration** tracking, elevation from live grade
- **Auto-pause** with delay and yellow visual indicator
- **Virtual routes** — fixed athletics track or a random pick from your GPX pool
- **SAVE button** → exports `.tcx`/`.fit` and auto-uploads to Strava (offline queue)

---

## 🔧 Treadmill Notes

**NordicTrack Commercial 2450 (NTL19124)**

- Uses sensorless speed control via MC1648DLS (back-EMF estimation)
- The displayed speed is a **commanded setpoint**, not a direct measurement
- Accuracy is approximately ±2.3%
- This project adds **independent speed measurement** via belt-mounted magnets and a Hall-effect sensor

---

## 🛠️ Firmware Notes

The Feather nRF52840 runs the **S340 SoftDevice** (not S140), which provides both BLE and ANT+ support. Key points:

- Application flash starts at `0x31000` (S340 memory map)
- Communicates with Raspberry Pi over **USB serial** (not BLE)
- Broadcasts speed data over **BLE** to Garmin Fenix 8
- LSM303DLHC (accel/mag) shares the main sensor PCB
- Arduino IDE with Adafruit nRF52 BSP (modified for S340)
- J-Link EDU Mini used for initial SoftDevice flashing and recovery
- UF2 uploads work normally after initial setup

### S340 SoftDevice Setup

The S340 is a closed-source SoftDevice distributed by Garmin Canada. To flash it, follow the instructions in this merged PR on the Adafruit nRF52 Bootloader repo:

> **[feat: add support for SoftDevice S340 — PR #359](https://github.com/adafruit/Adafruit_nRF52_Bootloader/pull/359)**

In short:
1. Register an **ANT+ Adopter** account at [thisisant.com](https://www.thisisant.com/register/) (access granted within ~1 business day)
2. Download the **S340 v7.0.1** SoftDevice from the ANT+ resources page
3. Follow the PR's `readme.md` to place and rename the files in the bootloader repo
4. Uncomment the evaluation key in `nrf_sdm.h` (line 191)
5. Flash the bootloader with `SD_NAME=s340` using a J-Link

> ⚠️ The evaluation key is for development only. A commercial license key from Garmin/Dynastream is required for any product release.

---

## 🗺️ Roadmap

- [x] Random GPS route generation for non-interval Strava exports (Quebec + worldwide)
- [x] Web-based dashboard (Python HTTP server + React kiosk UI)
- [x] Real cadence detection from the frame accelerometer (Garmin footpod parity)
- [ ] Improved enclosure design with snap-fit

---

## 📝 License

This project is licensed under the GNU General Public License v3.0 — see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgements

- [Adafruit](https://www.adafruit.com/) — Feather nRF52840, sensors, and learning resources
- [JLCPCB](https://jlcpcb.com/) — PCB fabrication and SMT assembly
- [bleak](https://github.com/hbldh/bleak) — Python BLE library
- [Strava API](https://developers.strava.com/) — Activity upload integration

---

<p align="center">
  Built with ❤️ on a treadmill in Montréal
</p>
