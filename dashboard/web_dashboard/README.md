# Treadmill Dashboard — Web edition (v9)

Single UI, served from the Pi to a Chromium kiosk window.

```
┌──────────────────────────────────────────────────────────────────┐
│  Pi 4 (always on)                                                │
│                                                                  │
│   Feather (USB serial)  ─────┐                                   │
│   HR monitor (BLE)      ─────┤                                   │
│                              ▼                                   │
│                  treadmill_server.py  ◄── systemd service        │
│                  · reads sensors, smooths, logs CSV              │
│                  · /api/state  (10 Hz polled)                    │
│                  · /api/cmd    (POST actions)                    │
│                  · serves /static/*                              │
│                              │                                   │
│                              │ HTTP localhost:8080               │
│                              ▼                                   │
│                  Chromium --kiosk (autostart)                    │
│                  · displays dashboard.html fullscreen            │
│                  · touch input → fetch /api/cmd                  │
└──────────────────────────────────────────────────────────────────┘
```

## Layout

```
web_dashboard/
├── treadmill_server.py     # HTTP server + sensor threads + business logic
├── static/                 # served at /static/* (and root file fallbacks)
│   ├── dashboard.html      # main page (live polling)
│   ├── tokens.jsx
│   ├── primitives.jsx
│   ├── layouts.jsx
│   └── screens.jsx         # IdleScreen, RunHeader, SaveScreen, CalibrationPage
└── kiosk/
    ├── treadmill-server.service   # systemd unit for the server
    ├── treadmill-kiosk.sh         # Chromium kiosk launcher
    └── treadmill-kiosk.desktop    # autostart entry
```

## Quick test on dev machine

```bash
cd web_dashboard
python3 treadmill_server.py --port 8080
# open http://localhost:8080 in any browser
```

The server still works if the Feather isn't connected — `/api/state`
just returns zeros and `usb_connected: false`.

## Deploy on the Pi

Adjust `pi` to your username if different.

```bash
# 1. Copy the project
mkdir -p ~/treadmill_dashboard
rsync -a /path/to/this/repo/  ~/treadmill_dashboard/

# 2. System packages
sudo apt update
sudo apt install -y python3-serial chromium-browser unclutter curl

# 3. (Optional but recommended) Python deps for HR monitor
pip3 install bleak    # if treadmill_hr.py needs it

# 4. Install the systemd service
sudo cp ~/treadmill_dashboard/web_dashboard/kiosk/treadmill-server.service \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now treadmill-server.service

# 5. Verify the server
curl http://localhost:8080/api/health
#  → {"ok": true, "ts": ...}

# 6. Install the Chromium autostart
mkdir -p ~/.config/autostart
cp ~/treadmill_dashboard/web_dashboard/kiosk/treadmill-kiosk.desktop \
   ~/.config/autostart/
chmod +x ~/treadmill_dashboard/web_dashboard/kiosk/treadmill-kiosk.sh

# 7. Reboot
sudo reboot
```

After reboot:
- the systemd service starts the HTTP server at boot,
- LXDE autostart launches Chromium kiosk pointing at `http://localhost:8080`,
- the dashboard is on screen full-bleed 800×480, touch-ready.

## Logs / debug

```bash
# Server logs
sudo journalctl -u treadmill-server.service -f

# Kiosk crashes? Run it manually from a terminal to see Chromium output:
~/treadmill_dashboard/web_dashboard/kiosk/treadmill-kiosk.sh

# Test the API by hand
curl http://localhost:8080/api/state | jq
curl -X POST -H 'Content-Type: application/json' \
  -d '{"action":"start"}' http://localhost:8080/api/cmd
```

## Exit kiosk during dev

`Alt+F4` closes Chromium. SSH from another machine and stop the autostart:
```bash
pkill -f "chromium.*kiosk"
```

## API reference

### `GET /api/state`
Returns the full snapshot used by the page. Polled every 100 ms. Notable fields:

| field | type | meaning |
|---|---|---|
| `screen` | `idle\|running\|paused\|save\|calibration` | controller-driven |
| `mode` | `piste\|random` | persisted in config |
| `live_speed` | km/h, smoothed + corrected | |
| `live_speed_raw` | km/h, raw from feather | |
| `live_incline` | %, smoothed + corrected | |
| `live_incline_raw` | %, raw — used by calibration UI | |
| `distance_m`, `elapsed_sec`, `cadence`, `elev_gain` | session stats | |
| `pace_factor`, `inclin_offset`, `inclin_points` | calibration values | |
| `auto_paused` | true if paused by inactivity | |
| `calib_state`, `calib_progress` | speed-calibration recording | |
| `usb_connected`, `ble_connected`, `hr_connected` | connection flags | |

### `POST /api/cmd`
Body: `{"action": "<name>", "args": {...}}`. All actions return `{ok: true}` or `{ok: false, error: ...}`.

| action | args | effect |
|---|---|---|
| `start` | — | start a new activity, switch to `running` |
| `pause` / `resume` / `stop` | — | session controls |
| `save` / `discard` | — | from save screen → idle |
| `resume_from_save` | — | go back to running |
| `set_screen` | `{screen}` | navigate to `idle` or `calibration` |
| `set_mode` | `{mode}` | persist piste/random |
| `toggle_ble` | — | flip BLE on/off |
| `pace_delta` | `{delta}` | adjust pace_factor live |
| `incline_delta` | `{delta}` | adjust inclin_offset live |
| `ref_speed_delta` | `{delta}` | calibration reference value |
| `start_speed_calibration` | — | begin 10s recording |
| `reset_pace` | — | pace_factor → 1.000 |
| `zero_incline` | — | snapshot current raw as zero |
| `reset_incline` | — | clear offset + points |
| `invert_incline` | `{on: bool}` | flip sign |
| `capture_incline_point` | `{target}` | memorise raw at target |
| `clear_incline_points` | — | wipe interpolation table |
