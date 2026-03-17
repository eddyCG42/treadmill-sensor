# 🏃 Treadmill Sensor

> DIY speed & heart-rate dashboard for the NordicTrack Commercial 2450, with Garmin Fenix 8 integration, live BLE heart rate, auto-pause detection, and one-tap Strava upload.

<!-- Badges — update eddyCG42 after pushing -->
![License](https://img.shields.io/badge/license-GPL%20v3-blue)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-C51A4A?logo=raspberrypi)
![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)
![Firmware](https://img.shields.io/badge/firmware-nRF52840%20%2B%20S340-00A9CE)
![PCB](https://img.shields.io/badge/PCB-JLCPCB-green)

---

<!-- TODO: Add a hero photo or GIF of the dashboard running on the treadmill -->
<!-- ![Dashboard in action](docs/images/hero.jpg) -->

## ✨ Features

| Feature | Details |
|---|---|
| **Live Speed Tracking** | DRV5023 Hall-effect sensor (separate PCB) → Feather nRF52840 → Pi via USB serial |
| **Motion / Orientation** | LSM303DLHC accelerometer/magnetometer on main Feather PCB |
| **Heart Rate** | Garmin HRM-Pro Plus chest strap → Pi via BLE (`bleak`) + Fenix 8 via ANT+ simultaneously |
| **Garmin Fenix 8 Integration** | Feather broadcasts speed over BLE to Fenix 8; HRM-Pro Plus sends HR over ANT+ |
| **Dashboard** | Real-time Python + HyperPixel display (SPI), mounted on treadmill arm |
| **Auto-Pause** | Detects belt stop → yellow "AUTO-PAUSE" state (5 s delay) |
| **Strava Upload** | One-tap SAVE → auto-upload as treadmill activity via Strava API |
| **Custom PCBs** | Hall sensor PCB (60 mm, JLCPCB) + main sensor PCB (Feather + LSM303DLHC) |
| **3D-Printable Enclosures** | OpenSCAD: Pi + HyperPixel arm clamp & Feather sensor mount |

---

## 📐 Architecture

```
┌──────────────┐  wire   ┌──────────────────┐  USB     ┌──────────────────┐  SPI    ┌────────────┐
│  DRV5023     │ ──────► │  Feather         │ serial   │  Raspberry Pi    │ ──────► │ HyperPixel │
│  Hall Sensor │         │  nRF52840        │ ───────► │  (EddyPi)        │         │  Display   │
│  (separate   │         │  + LSM303DLHC    │         │                  │         └────────────┘
│   PCB)       │         │  (main PCB)      │         │  Dashboard v8    │
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

---

## 🧰 Hardware

See the full **[Bill of Materials](docs/BOM.md)** for part numbers and sourcing.

### Core Components

- **Raspberry Pi** (3B+ or 4) — dashboard host (`EddyPi`), receives speed data via USB serial, HR via BLE
- **Adafruit Feather nRF52840** — sensor MCU on main PCB, running S340 SoftDevice (BLE + ANT+), communicates with Pi over USB serial and broadcasts speed to Garmin Fenix 8 over BLE
- **LSM303DLHC** — accelerometer/magnetometer, on the same main PCB as the Feather
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

**Main Sensor PCB** — carries the Feather nRF52840 + LSM303DLHC:
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

The dashboard connects via BLE using `bleak`. Update the MAC address in `treadmill_hr.py` to match your strap.

### 5. Install the Systemd Service

```bash
sudo cp systemd/treadmill-dashboard.service /etc/systemd/system/
sudo systemctl enable treadmill-dashboard
sudo systemctl start treadmill-dashboard
```

### 6. Flash the Feather Firmware

See [Firmware Notes](#-firmware-notes) below and the [Adafruit nRF52 Bootloader S340 PR](https://github.com/adafruit/Adafruit_nRF52_Bootloader/pull/359) for Arduino IDE build instructions and S340 SoftDevice setup.

---

## 📁 Project Structure

```
treadmill-sensor/
├── README.md
├── LICENSE
├── .gitignore
├── docs/
│   ├── images/                  ← Photos, screenshots, diagrams
│   ├── BOM.md                   ← Bill of materials
│   └── wiring.md                ← Wiring guide
├── firmware/
│   └── feather-nrf52840/        ← Arduino sketch (S340 SoftDevice)
├── dashboard/
│   ├── treadmill_dashboard_v8.py
│   ├── treadmill_hr.py
│   ├── treadmill_export.py
│   ├── treadmill_strava.py
│   └── requirements.txt
├── enclosures/
│   ├── pi-hyperpixel-clamp.scad
│   └── feather-sensor-mount.scad
├── pcb/
│   └── easyeda/                 ← Gerber exports, schematic PDF
├── systemd/
│   └── treadmill-dashboard.service
└── strava_config.json.example   ← Template (no secrets)
```

---

## 📊 Dashboard

<!-- TODO: Add screenshot of the dashboard UI -->
<!-- ![Dashboard Screenshot](docs/images/dashboard-screenshot.png) -->

The dashboard (`treadmill_dashboard_v8.py`) provides:

- **Live speed** from the Hall-effect sensor (via Feather → USB serial)
- **Heart rate** graph from HRM-Pro Plus (BLE via `bleak`)
- **Distance & duration** tracking
- **Auto-pause** with 5-second delay and yellow visual indicator
- **SAVE button** → exports `.fit`/`.tcx` and auto-uploads to Strava

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

- [ ] Random GPS route generation for non-interval Strava exports (Quebec + worldwide)
- [ ] Improved enclosure design with snap-fit
- [ ] Web-based dashboard alternative (Flask/FastAPI)

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
