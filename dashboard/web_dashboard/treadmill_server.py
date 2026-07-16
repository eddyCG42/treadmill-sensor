#!/usr/bin/env python3
"""
Treadmill Dashboard HTTP server.

Reads sensors from the Feather (USB serial) and the HR monitor (BLE),
exposes them as JSON, serves the dashboard, accepts commands.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hmac
import ipaddress
import json
import logging
import math
import os
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

import serial

# Allow importing sibling helper modules from the parent directory.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

try:
    from treadmill_export import export_activity
except ImportError:
    export_activity = None
try:
    from treadmill_strava import is_configured as strava_configured
except ImportError:
    def strava_configured() -> bool:
        return False
try:
    import treadmill_strava_queue as strava_queue
except ImportError:
    strava_queue = None
try:
    from treadmill_hr import start_hr_monitor, get_hr
except ImportError:
    start_hr_monitor = None

    def get_hr():
        return {"bpm": 0, "connected": False, "device_name": ""}


# =====================================================================
# Configuration
# =====================================================================

CONFIG_FILE = os.path.expanduser("~/treadmill_config.json")
LOG_DIR = os.path.expanduser("~/treadmill_logs")
LOG_MAX_MB = 500
SERIAL_BAUD = 115200
STATIC_DIR = os.path.join(HERE, "static")
MAX_REQUEST_BODY_BYTES = 16 * 1024
DEFAULT_HOST = "127.0.0.1"
ALLOW_PRIVATE_NETWORKS = os.environ.get("TREADMILL_ALLOW_PRIVATE_NET", "1") != "0"
API_TOKEN = os.environ.get("TREADMILL_API_TOKEN", "").strip()
SENSOR_STALE_AFTER_S = 3.0

logging.basicConfig(
    level=os.environ.get("TREADMILL_LOG_LEVEL", "INFO").upper(),
    format="[%(name)s] %(levelname)s: %(message)s",
)
LOGGER = logging.getLogger("treadmill_server")

CONFIG_DEFAULTS = {
    "pace_factor": 1.0,
    "inclin_offset": 0.0,
    "inclin_points": {},
    "calib_speed_ref": 0.0,
    "calib_speed_raw": 0.0,
    "route_mode": "piste",
    # Feather's OWN footfall detector (threshold counter) — kept only as a
    # stale-push fallback now that the Pi runs the real detector. Synced on connect.
    "cad_threshold": 2.0,
    "cad_rearm": 1.0,
    "cad_refract_ms": 180,
    "cad_timeout_ms": 2000,
    # Pi-side cadence detector (band-pass + windowed autocorrelation on the $RAW
    # stream). These are NOT Feather SET keys — read live by treadmill_cadence,
    # so every one is tunable at runtime without re-flashing the board.
    "cad_det_enable": True,
    "cad_win_s": 4.0,
    "cad_hop_s": 0.25,
    "cad_band_lo_hz": 0.7,
    "cad_band_hi_hz": 4.5,
    "cad_conf_min": 0.18,
    "cad_sl_min_m": 0.35,
    "cad_sl_max_m": 2.8,
    "cad_hold_ms": 2500,
    "cad_smooth": 0.30,
    # Feather runtime tunables. All pushed to the Feather on connect via
    # `SET <key> <value>`, so changing any of them is a config edit on the Pi —
    # never a re-flash of the sealed board. Key names match the firmware's.
    "wheel_diam_mm": 48.0,
    "debounce_ms": 15,
    "speed_timeout_ms": 3000,
    "ble_update_ms": 500,
    "serial_update_ms": 500,
    "speed_ema": 0.25,
    "tof_ema": 0.10,
    "accel_base_ema": 0.02,
    # Athlete profile (Pi-side): HR strap MAC, HR zone bounds, body mass.
    "hr_mac": "",
    "hr_max": 198,
    "hr_rest": 64,
    "body_mass_kg": 70.0,
}

# Firmware SET keys that are plain numeric runtime params (excludes the ToF
# calibration points, which are derived from inclin_points below).
FEATHER_PARAM_KEYS = (
    "pace_factor", "inclin_offset", "cad_threshold", "cad_rearm",
    "cad_refract_ms", "cad_timeout_ms", "wheel_diam_mm", "debounce_ms",
    "speed_timeout_ms", "ble_update_ms", "serial_update_ms",
    "speed_ema", "tof_ema", "accel_base_ema",
)

config_lock = threading.Lock()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


from treadmill_metrics import elevation_delta  # shared with treadmill_export
from treadmill_cadence import cadence_detector  # Pi-side footfall detector

try:
    import treadmill_routes  # random-pool catalogue for the home screen
except Exception:
    treadmill_routes = None


def _route_catalogue() -> tuple[int, float, float]:
    """(count, min_km, max_km) of the RANDOM pool (GPX in ~/treadmill_routes,
    excluding the builtin track). Cheap — treadmill_routes caches on dir mtime.
    Feeds the home screen so its RANDOM card stays truthful as routes change."""
    if not treadmill_routes:
        return 0, 0.0, 0.0
    try:
        gpx = [r for r in treadmill_routes.list_routes()
               if r.get("source") and r["source"] != "builtin"]
    except Exception:
        return 0, 0.0, 0.0
    if not gpx:
        return 0, 0.0, 0.0
    kms = [r["distance_km"] for r in gpx]
    return len(gpx), round(min(kms), 1), round(max(kms), 1)


def _safe_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return parsed


def sanitize_config(raw_cfg: dict[str, Any] | None) -> dict[str, Any]:
    src = raw_cfg if isinstance(raw_cfg, dict) else {}
    route_mode = src.get("route_mode", CONFIG_DEFAULTS["route_mode"])
    if route_mode not in {"piste", "random"}:
        route_mode = CONFIG_DEFAULTS["route_mode"]

    inclin_points: dict[str, float] = {}
    raw_points = src.get("inclin_points") or {}
    if isinstance(raw_points, dict):
        for key, raw_value in raw_points.items():
            target = _safe_float(key, math.nan)
            point = _safe_float(raw_value, math.nan)
            if not math.isfinite(target) or not math.isfinite(point):
                continue
            if -20.0 <= target <= 20.0 and -10000.0 <= point <= 10000.0:
                inclin_points[f"{target:.1f}"] = round(point, 2)

    cfg = dict(CONFIG_DEFAULTS)
    cfg.update(
        {
            "pace_factor": round(_clamp(_safe_float(src.get("pace_factor"), 1.0), 0.5, 2.0), 3),
            "inclin_offset": round(_clamp(_safe_float(src.get("inclin_offset"), 0.0), -20.0, 20.0), 2),
            "inclin_points": inclin_points,
            "calib_speed_ref": round(_clamp(_safe_float(src.get("calib_speed_ref"), 0.0), 0.0, 30.0), 2),
            "calib_speed_raw": round(_clamp(_safe_float(src.get("calib_speed_raw"), 0.0), 0.0, 30.0), 2),
            "route_mode": route_mode,
            "cad_threshold": round(_clamp(_safe_float(src.get("cad_threshold"), 2.0), 0.1, 50.0), 2),
            "cad_rearm": round(_clamp(_safe_float(src.get("cad_rearm"), 1.0), 0.05, 50.0), 2),
            "cad_refract_ms": int(_clamp(_safe_float(src.get("cad_refract_ms"), 180), 50, 1000)),
            "cad_timeout_ms": int(_clamp(_safe_float(src.get("cad_timeout_ms"), 2000), 300, 10000)),
            "wheel_diam_mm": round(_clamp(_safe_float(src.get("wheel_diam_mm"), 48.0), 5.0, 500.0), 2),
            "debounce_ms": int(_clamp(_safe_float(src.get("debounce_ms"), 15), 1, 200)),
            "speed_timeout_ms": int(_clamp(_safe_float(src.get("speed_timeout_ms"), 3000), 500, 20000)),
            "ble_update_ms": int(_clamp(_safe_float(src.get("ble_update_ms"), 500), 100, 5000)),
            "serial_update_ms": int(_clamp(_safe_float(src.get("serial_update_ms"), 500), 100, 5000)),
            "speed_ema": round(_clamp(_safe_float(src.get("speed_ema"), 0.25), 0.01, 1.0), 3),
            "tof_ema": round(_clamp(_safe_float(src.get("tof_ema"), 0.10), 0.01, 1.0), 3),
            "accel_base_ema": round(_clamp(_safe_float(src.get("accel_base_ema"), 0.02), 0.001, 1.0), 3),
            "hr_mac": str(src.get("hr_mac", "") or "").strip()[:32],
            "hr_max": int(_clamp(_safe_float(src.get("hr_max"), 198), 100, 240)),
            "hr_rest": int(_clamp(_safe_float(src.get("hr_rest"), 64), 30, 120)),
            "body_mass_kg": round(_clamp(_safe_float(src.get("body_mass_kg"), 70.0), 30.0, 200.0), 1),
        }
    )
    return cfg


def _atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            json.dump(payload, tmp_file, indent=2, sort_keys=True)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def load_config() -> dict:
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as exc:
            LOGGER.warning("Failed to load config %s: %s", CONFIG_FILE, exc)
    return sanitize_config(cfg)


def save_config(cfg: dict) -> None:
    cleaned = sanitize_config(cfg)
    with config_lock:
        _atomic_write_json(CONFIG_FILE, cleaned)


# =====================================================================
# Shared sensor state
# =====================================================================

sensor_data: dict[str, Any] = {
    "speed_kmh": 0.0,
    "speed_ms": 0.0,
    "cadence": 0,
    "distance_m": 0.0,
    "inclinaison": 0.0,
    "inclin_raw_smooth": 0.0,
    "pulses": 0,
    "ble_on": True,
    "ble_connected": False,
    "serial_connected": False,
    "last_update": 0.0,
    "tof_ok": False,           # unknown until a $DATA line says otherwise
    "cad_dynamic": 0.0,
    "cad_steps": 0,
    "imu_ok": False,
    "cad_real_spm": 0,
    "fw_version": "",
}
data_lock = threading.Lock()
serial_port: Optional[serial.Serial] = None
serial_lock = threading.Lock()


# =====================================================================
# Serial layer
# =====================================================================

ADAFRUIT_USB_VID = 0x239A


def find_serial_port() -> Optional[str]:
    try:
        from serial.tools import list_ports

        for port in list_ports.comports():
            if getattr(port, "vid", None) == ADAFRUIT_USB_VID:
                return port.device
    except ImportError:
        pass

    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        ports = glob.glob(pattern)
        if ports:
            return ports[0]
    return None


def send_command(cmd: str) -> bool:
    with serial_lock:
        if serial_port and serial_port.is_open:
            try:
                serial_port.write(f"{cmd}\n".encode("utf-8"))
                serial_port.flush()
                return True
            except Exception:
                LOGGER.warning("Serial write failed for command %s", cmd, exc_info=True)
                return False
    LOGGER.debug("Dropped serial command with no active port: %s", cmd)
    return False


def push_param(key: str, value: Any) -> bool:
    """Best-effort runtime push of a single Feather parameter (fire-and-forget,
    no ack). Used by the live config handlers. The authoritative, verified sync
    happens at connect in the serial thread (sync_config_to_feather)."""
    return send_command(f"SET {key} {value}")


def _format_param(cfg: dict, key: str) -> str:
    """Serialise a config value the way the firmware SET parser expects."""
    v = cfg.get(key, CONFIG_DEFAULTS.get(key, 0))
    if key in ("cad_refract_ms", "cad_timeout_ms", "debounce_ms",
               "speed_timeout_ms", "ble_update_ms", "serial_update_ms"):
        return str(int(v))
    return f"{float(v):.3f}"


def feather_param_items(cfg: dict) -> list[tuple[str, str]]:
    """The full (firmware_key, value_str) list to push on connect: numeric
    tunables plus the two ToF calibration points derived from inclin_points."""
    items = [(k, _format_param(cfg, k)) for k in FEATHER_PARAM_KEYS]
    pts = cfg.get("inclin_points") or {}
    for key, raw_mm in pts.items():
        try:
            target = float(key)
            mm = int(round(float(raw_mm)))
        except (TypeError, ValueError):
            continue
        if abs(target) < 0.01:
            items.append(("tof_cal_0_mm", str(mm)))
        elif abs(target - 12.0) < 0.01:
            items.append(("tof_cal_12_mm", str(mm)))
    return items


def _readline(sp: "serial.Serial") -> Optional[str]:
    """One decoded line, "" on timeout, None on error."""
    try:
        return sp.readline().decode("utf-8", errors="ignore").strip()
    except Exception:
        return None


def _feed_cadence(line: str) -> None:
    """Feed one `$RAW,ms,ax,ay,az,speed_mms` sample to the Pi-side detector.
    Integers are mm/s² and mm/s from the firmware; the detector wants m/s²/m/s."""
    try:
        p = line[5:].split(",")
        cadence_detector.feed(int(p[0]), int(p[1]) / 1000.0, int(p[2]) / 1000.0,
                              int(p[3]) / 1000.0, int(p[4]) / 1000.0, time.time())
    except (ValueError, IndexError):
        return


def _handle_serial_line(line: str) -> None:
    """Dispatch a single inbound serial line (telemetry / raw / hello / logs).
    Shared by the main read loop and the connect-time sync so telemetry keeps
    flowing while we wait for config acks."""
    if not line:
        return
    if line.startswith("$RAW,"):
        raw_recorder.handle(line)
        _feed_cadence(line)
        return
    if line.startswith("$HELLO,"):
        fw = line.split(",", 1)[1].strip()
        with data_lock:
            sensor_data["fw_version"] = fw
        LOGGER.info("Feather firmware %s", fw)
        return
    parsed = parse_data_line(line)
    if parsed:
        with data_lock:
            sensor_data.update(parsed)
            sensor_data["serial_connected"] = True
            sensor_data["last_update"] = time.time()
    elif line.startswith("[") or line.startswith("$"):
        LOGGER.info("feather %s", line)


def wait_for_feather_ready(sp: "serial.Serial", timeout: float = 6.0) -> bool:
    """Block until the Feather is reading serial: it prints `[BOOT] READY` at
    the end of setup(). A `STATUS` nudge makes an already-running Feather (Pi
    reconnect without a Feather reboot) answer immediately instead of waiting
    out the timeout. Returns True if we saw it come alive."""
    send_command("STATUS")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = _readline(sp)
        if line is None:
            return False
        if line.startswith("[BOOT] READY") or line.startswith("[STATUS]"):
            _handle_serial_line(line)
            return True
        _handle_serial_line(line)
    return False


def _sync_one(sp: "serial.Serial", key: str, value: str,
              tries: int = 3, per_try_s: float = 0.6) -> bool:
    """Send `SET key value` and wait for the Feather's `[CONFIG] key=` echo,
    retrying a few times. Returns True once acked."""
    prefix = f"[CONFIG] {key}="
    for _ in range(tries):
        send_command(f"SET {key} {value}")
        deadline = time.monotonic() + per_try_s
        while time.monotonic() < deadline:
            line = _readline(sp)
            if line is None:
                return False
            if line.startswith(prefix):
                return True
            if line.startswith(f"[CONFIG] rejected {key}"):
                LOGGER.warning("Feather rejected SET %s=%s", key, value)
                return False
            _handle_serial_line(line)
    LOGGER.warning("No ack for SET %s=%s after %d tries", key, value, tries)
    return False


def sync_config_to_feather(cfg: dict, sp: "serial.Serial | None" = None) -> None:
    """Push every Feather parameter. With `sp` (connect-time, in the serial
    thread) each SET is verified against its `[CONFIG]` echo and retried. Without
    `sp` it's a best-effort blind push (kept for safety; runtime handlers use
    push_param for single values)."""
    items = feather_param_items(cfg)
    if sp is None:
        for key, value in items:
            send_command(f"SET {key} {value}")
        return
    acked = sum(1 for key, value in items if _sync_one(sp, key, value))
    LOGGER.info("Feather config synced: %d/%d params acked", acked, len(items))


def parse_data_line(line: str) -> Optional[dict]:
    try:
        parts = line.split(",")
        if len(parts) >= 9 and parts[0] == "$DATA":
            parsed = {
                "speed_kmh": float(parts[1]),
                "speed_ms": float(parts[2]),
                "cadence": int(parts[3]),
                "distance_m": float(parts[4]),
                "inclinaison": float(parts[5]),
                "pulses": int(parts[6]),
                "ble_on": parts[7] == "1",
                "ble_connected": parts[8].strip() == "1",
            }
            if len(parts) >= 10:
                parsed["inclin_raw_smooth"] = float(parts[9].strip())
            if len(parts) >= 11:
                parsed["tof_ok"] = parts[10].strip() == "1"
            else:
                parsed["tof_ok"] = True
            # Appended cadence-tuning telemetry (newer firmware only).
            if len(parts) >= 12:
                parsed["cad_dynamic"] = float(parts[11].strip())
            if len(parts) >= 13:
                parsed["cad_steps"] = int(parts[12].strip())
            if len(parts) >= 14:
                parsed["imu_ok"] = parts[13].strip() == "1"
            if len(parts) >= 15:
                parsed["cad_real_spm"] = int(parts[14].strip())
            return parsed
    except Exception:
        LOGGER.debug("Failed to parse serial data line: %r", line, exc_info=True)
    return None


class RawRecorder:
    """Captures the Feather's high-rate `$RAW` accel stream to a CSV, for
    offline cadence-detection calibration. Separate from ActivityLogger — a
    different cadence (100 Hz), format, and lifecycle. Buffered, flushed once a
    second (never per-line) to stay SD-card friendly."""

    def __init__(self) -> None:
        self.file = None
        self.path: Optional[str] = None
        self.lines = 0
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def is_recording(self) -> bool:
        return self.file is not None

    def start(self) -> Optional[str]:
        with self._lock:
            if self.file is not None:
                return self.path
            os.makedirs(LOG_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.path = os.path.join(LOG_DIR, f"cadraw_{ts}.csv")
            self.file = open(self.path, "w", newline="", encoding="utf-8")
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self.file.write("pi_time,feather_ms,ax_mms2,ay_mms2,az_mms2,speed_mms\n")
            self.lines = 0
            self._last_flush = time.time()
            return self.path

    def handle(self, line: str) -> None:
        # line: "$RAW,<feather_ms>,<ax>,<ay>,<az>,<speed_mms>"
        with self._lock:
            if self.file is None:
                return
            body = line[5:] if line.startswith("$RAW,") else line
            try:
                self.file.write(f"{time.time():.6f},{body}\n")
                self.lines += 1
                now = time.time()
                if now - self._last_flush >= 1.0:
                    self.file.flush()
                    self._last_flush = now
            except OSError:
                pass

    def stop(self) -> tuple[Optional[str], int]:
        with self._lock:
            if self.file is None:
                return None, 0
            try:
                self.file.flush()
                self.file.close()
            except OSError:
                pass
            path, lines = self.path, self.lines
            self.file = None
            return path, lines


raw_recorder = RawRecorder()


def serial_thread_fn() -> None:
    global serial_port

    config_synced = False
    while True:
        port_name = find_serial_port()
        if not port_name:
            with data_lock:
                sensor_data["serial_connected"] = False
            config_synced = False
            time.sleep(2)
            continue

        sp = None
        try:
            sp = serial.Serial(port_name, SERIAL_BAUD, timeout=1)
            with serial_lock:
                serial_port = sp
            with data_lock:
                sensor_data["serial_connected"] = True

            if not config_synced:
                # Verified sync: wait for the Feather to be reading serial, then
                # push every param and confirm each against its [CONFIG] echo.
                # Short read timeout during sync so ack waits stay responsive.
                sp.timeout = 0.25
                if not wait_for_feather_ready(sp):
                    LOGGER.warning("Feather never signalled READY; syncing anyway")
                sync_config_to_feather(load_config(), sp)
                sp.timeout = 1
                config_synced = True
                # Turn on the continuous raw accel stream that feeds the Pi-side
                # cadence detector, and clear any stale window from a prior link.
                cadence_detector.reset()
                send_command("CAD_RAW:1")

            while sp.is_open:
                line = _readline(sp)
                if line is None:
                    break
                if not line:
                    continue
                _handle_serial_line(line)
        except Exception:
            LOGGER.warning("Serial loop failed on %s", port_name, exc_info=True)

        with serial_lock:
            if sp:
                try:
                    sp.close()
                except Exception:
                    LOGGER.debug("Serial close failed", exc_info=True)
            serial_port = None
        with data_lock:
            sensor_data["serial_connected"] = False
        config_synced = False
        time.sleep(2)


# =====================================================================
# Smoothing + activity logger
# =====================================================================

class DisplaySmoother:
    def __init__(self, alpha: float = 0.25):
        self.alpha = alpha
        self.s_speed = 0.0
        self.s_inclin = 0.0
        self.s_cadence = 0.0

    def update(self, speed_kmh: float, inclin: float, cadence: float) -> None:
        if speed_kmh < 0.1:
            self.s_speed *= 0.5
            if self.s_speed < 0.3:
                self.s_speed = 0.0
            self.s_cadence = 0
        else:
            if self.s_speed < 0.1:
                self.s_speed = speed_kmh
                self.s_cadence = cadence
            else:
                self.s_speed = self.alpha * speed_kmh + (1 - self.alpha) * self.s_speed
                self.s_cadence = self.alpha * cadence + (1 - self.alpha) * self.s_cadence
        self.s_inclin = self.alpha * inclin + (1 - self.alpha) * self.s_inclin

    def get(self) -> tuple[float, float, int]:
        return self.s_speed, self.s_inclin, int(round(self.s_cadence))


class ActivityLogger:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.file = None
        self.writer = None
        self.filepath: Optional[str] = None
        self.log_interval = 1.0
        self.last_log = 0.0
        self.log_count = 0
        self.logs_size_mb = 0.0
        self.disk_free_mb = 0.0
        self.refresh_disk_info()

    def refresh_disk_info(self) -> None:
        total = 0
        count = 0
        for filename in glob.glob(os.path.join(LOG_DIR, "run_*.csv")):
            try:
                total += os.path.getsize(filename)
                count += 1
            except Exception:
                LOGGER.debug("Failed to stat %s", filename, exc_info=True)
        self.logs_size_mb = total / (1024 * 1024)
        self.log_count = count
        try:
            st = os.statvfs(LOG_DIR)
            self.disk_free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        except Exception:
            self.disk_free_mb = 0.0
        self._cleanup_if_needed()

    def _cleanup_if_needed(self) -> None:
        if self.logs_size_mb <= LOG_MAX_MB:
            return
        files = sorted(glob.glob(os.path.join(LOG_DIR, "run_*.csv")), key=os.path.getmtime)
        while files and self.logs_size_mb > LOG_MAX_MB * 0.8:
            oldest = files.pop(0)
            try:
                size = os.path.getsize(oldest)
                os.remove(oldest)
                self.logs_size_mb -= size / (1024 * 1024)
                self.log_count -= 1
            except Exception:
                LOGGER.warning("Failed to cleanup log file %s", oldest, exc_info=True)

    def start(self) -> None:
        if self.file:
            self.discard()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filepath = os.path.join(LOG_DIR, f"run_{ts}.csv")
        self.file = open(self.filepath, "w", newline="", encoding="utf-8")
        try:
            os.chmod(self.filepath, 0o600)
        except OSError:
            pass
        self.writer = csv.writer(self.file)
        self.writer.writerow(
            [
                "timestamp",
                "elapsed_s",
                "speed_raw_kmh",
                "speed_smooth_kmh",
                "speed_corr_kmh",
                "cadence",
                "inclin_raw",
                "inclin_corr",
                "distance_m",
                "pulses",
                "pace_factor",
                "heart_rate",
            ]
        )
        self.last_log = 0.0

    def log(self, elapsed_s: float, raw: dict, cfg: dict, inclin_corr: float | None = None) -> None:
        now = time.time()
        if now - self.last_log < self.log_interval:
            return
        self.last_log = now
        if not self.writer:
            return

        pf = cfg["pace_factor"]
        io = cfg["inclin_offset"]
        hr = get_hr()
        if inclin_corr is None:
            inclin_corr = raw["inclinaison"] + io

        self.writer.writerow(
            [
                datetime.now().strftime("%H:%M:%S"),
                f"{elapsed_s:.1f}",
                f"{raw['speed_kmh']:.2f}",
                f"{raw['speed_ms'] * 3.6:.2f}",
                f"{raw['speed_kmh'] * pf:.2f}",
                raw["cadence"],
                f"{raw['inclinaison']:.1f}",
                f"{inclin_corr:.1f}",
                f"{raw['distance_m']:.2f}",
                raw["pulses"],
                f"{pf:.3f}",
                hr["bpm"],
            ]
        )
        self.file.flush()

    def save(self) -> Optional[str]:
        if self.file:
            self.file.close()
            self.file = None
            self.writer = None
            self.refresh_disk_info()
            return self.filepath
        return None

    def discard(self) -> None:
        path = self.filepath
        if self.file:
            self.file.close()
            self.file = None
            self.writer = None
        if path and os.path.exists(path):
            os.remove(path)
        self.filepath = None
        self.refresh_disk_info()


# =====================================================================
# App controller
# =====================================================================

class App:
    SCREEN_IDLE = "idle"
    SCREEN_RUN = "running"
    SCREEN_PAUSED = "paused"
    SCREEN_SAVE = "save"
    SCREEN_CALIB = "calibration"

    def __init__(self):
        self.cfg = load_config()
        self.smoother = DisplaySmoother()
        self.logger = ActivityLogger()

        self.screen = self.SCREEN_IDLE
        self.mode = self.cfg.get("route_mode", "piste")

        self.session_start: Optional[float] = None
        self.elapsed_at_pause = 0.0
        # Distance is accumulated Pi-side from per-tick deltas of the Feather's
        # cumulative counter, so a lost RESET_DIST or a Feather reboot mid-run
        # can't corrupt the recorded distance (it just contributes a 0 delta).
        self._dist_accum = 0.0
        self._last_raw_dist = 0.0
        self.elev_gain = 0.0
        self.elev_loss = 0.0
        self.prev_distance = 0.0
        self.hr_sum = 0
        self.hr_count = 0
        self.hr_max = 0

        self.auto_pause = True
        self.speed_zero_since = 0.0
        self.auto_paused = False
        self.AUTO_PAUSE_DELAY = 5.0

        self.ref_speed = 5.0
        self.calib_state = "idle"
        self.calib_record_start = 0.0
        self.calib_record_speeds: list[float] = []

        self.last_save_msg = ""
        self.last_save_msg_t = 0.0

        # Latest computed display values, produced by tick() at ~1 Hz and read
        # by snapshot(). Keeps the browser poll side-effect free.
        self._disp_speed = 0.0
        self._disp_inclin = 0.0
        self._disp_cadence = 0
        self._disp_distance = 0.0

        self._lock = threading.RLock()

    def _persist_cfg(self) -> None:
        self.cfg = sanitize_config(self.cfg)
        save_config(self.cfg)

    def _sensor_snapshot(self) -> dict[str, Any]:
        with data_lock:
            return dict(sensor_data)

    def _sensor_available(self, raw: dict[str, Any] | None = None) -> bool:
        state = raw if raw is not None else self._sensor_snapshot()
        return bool(state.get("serial_connected")) and (time.time() - state.get("last_update", 0.0) <= SENSOR_STALE_AFTER_S)

    def elapsed(self) -> float:
        with self._lock:
            return self._elapsed_unlocked()

    def _elapsed_unlocked(self) -> float:
        if self.screen == self.SCREEN_RUN and self.session_start is not None:
            return self.elapsed_at_pause + (time.time() - self.session_start)
        return self.elapsed_at_pause

    def tick(self) -> None:
        """Advance session state once (~1 Hz, dedicated thread).

        This is where recording actually happens: CSV logging, elevation,
        HR accumulation, auto-pause and calibration. It used to live inside
        snapshot(), so nothing was recorded unless a browser was polling
        /api/state — a locked phone or dropped Wi-Fi silently stopped the run.
        """
        raw = self._sensor_snapshot()
        with self._lock:
            if time.time() - raw.get("last_update", 0.0) > SENSOR_STALE_AFTER_S:
                raw = {**raw, "speed_kmh": 0.0, "speed_ms": 0.0, "cadence": 0}

            self._tick_calibration(raw)

            raw_speed = raw["speed_kmh"] * self.cfg["pace_factor"]
            # inclin_points map a ToF distance reading (millimetres) -> % incline.
            # The Feather sends the *smoothed* ToF mm as "inclinaison" (both this
            # and "inclin_raw_smooth" are millimetres despite the names — the
            # firmware does no % conversion on the serial link). Feed the smoothed
            # mm for a stable incline; the offset is a % and is added AFTER the
            # mm->% mapping. Below 2 points there's no calibration to apply.
            pts = self.cfg.get("inclin_points") or {}
            if len(pts) >= 2:
                base_inclin = self._apply_incline_points(raw["inclinaison"])
            else:
                base_inclin = raw["inclinaison"]
            raw_inclin = _clamp(base_inclin + self.cfg["inclin_offset"], -50.0, 50.0)
            # Cadence: the Pi-side detector drives it whenever it holds a
            # confident reading (running, brisk walk); it falls back to the
            # Feather's speed-based broadcast when unsure (very slow walk, a
            # step-off, or a dropped stream) rather than showing a bad 0.
            det_spm, det_conf, det_fresh = cadence_detector.cadence(time.time())
            self._disp_cad_conf = det_conf
            cad_in = det_spm if det_fresh else raw["cadence"]
            self.smoother.update(raw_speed, raw_inclin, cad_in)
            speed, inclin, cadence = self.smoother.get()
            inclin = round(inclin * 2.0) / 2.0

            # Distance: accumulate the Feather counter's delta (immune to a
            # counter reset/reboot — a negative or huge jump contributes 0).
            rd = raw["distance_m"]
            d_raw = rd - self._last_raw_dist
            self._last_raw_dist = rd
            if self.screen == self.SCREEN_RUN and 0.0 <= d_raw < 1000.0:
                self._dist_accum += d_raw * self.cfg["pace_factor"]
            distance = self._dist_accum

            self._disp_speed = speed
            self._disp_inclin = inclin
            self._disp_cadence = cadence
            self._disp_distance = distance

            # Push the Pi's 6-point calibrated incline to the Feather so Garmin
            # shows the SAME grade as the dashboard (the firmware relays it and
            # falls back to its own mapping only if this push goes stale).
            if self._sensor_available(raw):
                send_command(f"BLE_INCLIN:{inclin:.2f}")
                # Push the Pi-detected cadence the same way (firmware broadcasts
                # it to Garmin while fresh). Only once the detector is live, so a
                # cold start leaves the Feather on its own fallback.
                if det_fresh:
                    send_command(f"BLE_CAD:{det_spm}")
                # Re-arm the raw stream well before the firmware's 60-min auto-off.
                now_t = time.time()
                if now_t - getattr(self, "_last_cadraw_arm", 0.0) > 600:
                    send_command("CAD_RAW:1")
                    self._last_cadraw_arm = now_t

            self._tick_auto_pause(speed)
            if self.screen == self.SCREEN_RUN:
                elapsed = self._elapsed_unlocked()
                self.logger.log(elapsed, raw, self.cfg, inclin_corr=raw_inclin)
                delta = distance - self.prev_distance
                self.prev_distance = distance
                d_alt = elevation_delta(delta, inclin)
                if d_alt > 0:
                    self.elev_gain += d_alt
                elif d_alt < 0:
                    self.elev_loss += -d_alt

            # HR: only fold a fresh reading into the average — a strap that
            # dropped keeps its last bpm for up to the silence window, which
            # would otherwise pollute the average.
            hr = get_hr()
            hr_fresh = (time.time() - hr.get("last_update", 0.0)) <= 5.0
            if self.screen == self.SCREEN_RUN and hr["bpm"] > 0 and hr_fresh:
                self.hr_sum += hr["bpm"]
                self.hr_count += 1
                if hr["bpm"] > self.hr_max:
                    self.hr_max = hr["bpm"]

    def snapshot(self, raw: dict) -> dict:
        """Read-only view for /api/state. No side effects — see tick()."""
        route_count, route_min_km, route_max_km = _route_catalogue()
        with self._lock:
            if time.time() - raw.get("last_update", 0.0) > SENSOR_STALE_AFTER_S:
                raw = {**raw, "speed_kmh": 0.0, "speed_ms": 0.0, "cadence": 0}

            hr = get_hr()
            hr_avg = (self.hr_sum // self.hr_count) if self.hr_count else 0

            return {
                "screen": self.screen,
                "mode": self.mode,
                "hr_avg": hr_avg,
                "hr_max": self.hr_max,
                "ble_on": raw.get("ble_on", True),
                "ble_connected": raw.get("ble_connected", False),
                "usb_connected": raw.get("serial_connected", False),
                "tof_ok": raw.get("tof_ok", False),
                "fw_version": raw.get("fw_version", ""),
                "live_speed": round(self._disp_speed, 2),
                "live_speed_raw": round(raw.get("speed_kmh", 0.0), 2),
                "live_incline": round(self._disp_inclin, 2),
                # Smoothed ToF mm — the same value cmd_capture_incline_point
                # stores, so "LIVE BRUT" shows exactly what a capture would grab.
                "live_incline_raw": round(raw.get("inclinaison", 0.0), 2),
                "live_hr": hr["bpm"],
                "hr_connected": hr["connected"],
                "distance_m": round(self._disp_distance, 2),
                "elapsed_sec": round(self._elapsed_unlocked(), 1),
                "cadence": self._disp_cadence,
                "elev_gain": round(self.elev_gain, 1),
                "elev_loss": round(self.elev_loss, 1),
                "pace_factor": round(self.cfg["pace_factor"], 3),
                "inclin_offset": round(self.cfg["inclin_offset"], 2),
                "inclin_points": self.cfg.get("inclin_points", {}),
                # Random-pool catalogue for the home screen's RANDOM card.
                "route_count": route_count,
                "route_min_km": route_min_km,
                "route_max_km": route_max_km,
                "hr_zone_max": int(self.cfg.get("hr_max", 198)),
                "hr_zone_rest": int(self.cfg.get("hr_rest", 64)),
                "auto_paused": self.auto_paused,
                "ref_speed": self.ref_speed,
                "calib_state": self.calib_state,
                "calib_progress": self._calib_progress(),
                # Cadence tuning: live impact signal + REAL measured spm (not
                # the broadcast value, which may be the synthetic fallback) +
                # config. cad_real_spm falls back to the broadcast cadence for
                # older firmware that doesn't send it.
                "cad_dynamic": round(raw.get("cad_dynamic", 0.0), 2),
                "cad_spm": raw.get("cad_real_spm", raw.get("cadence", 0)),
                # Pi-side detector: the authoritative live cadence + how sure it
                # is (ACF peak height). Confidence ~0.5+ = a locked rhythm.
                "cad_det_conf": round(getattr(self, "_disp_cad_conf", 0.0), 2),
                "cad_steps": raw.get("cad_steps", 0),
                "imu_ok": raw.get("imu_ok", False),
                "cad_threshold": round(self.cfg.get("cad_threshold", 2.0), 2),
                "cad_refract_ms": int(self.cfg.get("cad_refract_ms", 180)),
                "raw_recording": raw_recorder.is_recording(),
                "raw_lines": raw_recorder.lines,
                "log_count": self.logger.log_count,
                "logs_size_mb": round(self.logger.logs_size_mb, 1),
                "disk_free_mb": round(self.logger.disk_free_mb, 1),
                "last_save_msg": self.last_save_msg if (time.time() - self.last_save_msg_t < 4) else "",
                "now": datetime.now().strftime("%H:%M"),
                "strava": (
                    strava_queue.status()
                    if strava_queue
                    else {"pending_count": 0, "last_error": "", "last_error_at": 0, "last_success_at": 0, "oldest_pending": 0}
                ),
            }

    def _calib_progress(self) -> float:
        if self.calib_state != "recording":
            return 0.0
        return max(0.0, min(1.0, (time.time() - self.calib_record_start) / 10.0))

    def _apply_incline_points(self, value: float) -> float:
        pts = self.cfg.get("inclin_points") or {}
        if len(pts) < 2:
            return value

        pairs = []
        for key, raw in pts.items():
            try:
                pairs.append((float(raw), float(key)))
            except (TypeError, ValueError):
                continue
        if len(pairs) < 2:
            return value
        pairs.sort(key=lambda item: item[0])

        if value <= pairs[0][0]:
            (r0, t0), (r1, t1) = pairs[0], pairs[1]
            if r1 == r0:
                return t0
            return t0 + (value - r0) * (t1 - t0) / (r1 - r0)

        if value >= pairs[-1][0]:
            (r0, t0), (r1, t1) = pairs[-2], pairs[-1]
            if r1 == r0:
                return t1
            return t0 + (value - r0) * (t1 - t0) / (r1 - r0)

        for idx in range(len(pairs) - 1):
            r0, t0 = pairs[idx]
            r1, t1 = pairs[idx + 1]
            if r0 <= value <= r1:
                if r1 == r0:
                    return t0
                return t0 + (value - r0) * (t1 - t0) / (r1 - r0)
        return value

    def _tick_auto_pause(self, speed: float) -> None:
        if not self.auto_pause:
            return
        now = time.time()
        if speed < 0.2:
            if self.speed_zero_since == 0.0:
                self.speed_zero_since = now
            elif self.screen == self.SCREEN_RUN and now - self.speed_zero_since >= self.AUTO_PAUSE_DELAY:
                self._pause(auto=True)
        else:
            self.speed_zero_since = 0.0
            if self.screen == self.SCREEN_PAUSED and self.auto_paused:
                self._resume()

    def _tick_calibration(self, raw: dict) -> None:
        if self.calib_state != "recording":
            return
        if raw["speed_kmh"] > 0.1:
            self.calib_record_speeds.append(raw["speed_kmh"])
        if time.time() - self.calib_record_start >= 10.0:
            if len(self.calib_record_speeds) > 3:
                avg = sum(self.calib_record_speeds) / len(self.calib_record_speeds)
                if avg > 0.1:
                    self.cfg["pace_factor"] = self.ref_speed / avg
                    self.cfg["calib_speed_ref"] = self.ref_speed
                    self.cfg["calib_speed_raw"] = avg
                    self._persist_cfg()
                    push_param("pace_factor", f"{self.cfg['pace_factor']:.3f}")
                    self._note(f"Calibration x{self.cfg['pace_factor']:.3f}")
            self.calib_state = "idle"

    def _pause(self, auto: bool = False) -> None:
        if self.screen != self.SCREEN_RUN:
            return
        if self.session_start is not None:
            self.elapsed_at_pause += time.time() - self.session_start
        self.session_start = None
        self.auto_paused = auto
        self.screen = self.SCREEN_PAUSED

    def _resume(self) -> None:
        if self.screen not in {self.SCREEN_PAUSED, self.SCREEN_SAVE}:
            return
        if not self._sensor_available():
            self._note("Capteur USB indisponible")
            return
        self.session_start = time.time()
        self.auto_paused = False
        self.screen = self.SCREEN_RUN

    def _start(self) -> None:
        if self.screen == self.SCREEN_RUN:
            return
        # A run is paused or awaiting save with its CSV still open. Starting
        # would discard() it (delete from disk) with no confirmation — refuse.
        # The user must resume or explicitly save/discard first.
        if self.screen in {self.SCREEN_PAUSED, self.SCREEN_SAVE} or self.logger.file is not None:
            self._note("Course en cours - reprendre, sauver ou supprimer d'abord")
            return
        if time.time() < 1700000000:
            self._note("Horloge non synchronisee")
            return

        raw = self._sensor_snapshot()
        if not self._sensor_available(raw):
            self._note("Feather USB non connecte")
            return

        if len(self.cfg.get("inclin_points") or {}) < 2:
            self._note("Calibration inclinaison conseillee (>=2 points)")

        self.session_start = time.time()
        self.elapsed_at_pause = 0.0
        # Baseline distance accumulation on the Feather's current counter value
        # (no RESET_DIST round-trip to depend on — see tick()).
        self._dist_accum = 0.0
        self._last_raw_dist = raw["distance_m"]
        self.elev_gain = 0.0
        self.elev_loss = 0.0
        self.prev_distance = 0.0
        self.speed_zero_since = 0.0
        self.auto_paused = False
        self.hr_sum = 0
        self.hr_count = 0
        self.hr_max = 0
        self.logger.start()
        self.screen = self.SCREEN_RUN

    def _reset_session(self) -> None:
        self.screen = self.SCREEN_IDLE
        self.session_start = None
        self.elapsed_at_pause = 0.0
        self._dist_accum = 0.0
        self._last_raw_dist = 0.0
        self.elev_gain = 0.0
        self.elev_loss = 0.0
        self.prev_distance = 0.0
        self.auto_paused = False
        self.speed_zero_since = 0.0
        self.calib_state = "idle"
        self.calib_record_speeds = []

    def _save_and_export(self) -> None:
        path = self.logger.save()
        if path and export_activity:
            try:
                route_id = "piste_martinique" if self.mode == "piste" else "random"
                summary = None
                try:
                    tcx_path, summary = export_activity(path, route_id=route_id)
                except TypeError:
                    tcx_path, summary = export_activity(path)
                if not tcx_path:
                    # export_activity signals failure by RETURNING (None, msg),
                    # not raising — don't claim success or the run silently
                    # never reaches Strava and isn't queued for retry.
                    LOGGER.warning("Export produced no TCX for %s: %s", path, summary)
                    self._note("Sauvegarde locale OK, export KO")
                    return
                if strava_queue and strava_configured():
                    # Pass the chosen-route title so Strava shows e.g.
                    # "Central Park · Soir · Treadmill" instead of the raw
                    # "Treadmill Run <timestamp>" fallback.
                    name = summary.get("strava_name") if isinstance(summary, dict) else None
                    strava_queue.enqueue(tcx_path, name=name)
                self._note("Activite sauvegardee")
            except Exception:
                LOGGER.exception("Export failed for %s", path)
                self._note("Sauvegarde locale OK export KO")
        else:
            self._note("Activite sauvegardee")

    def _note(self, msg: str) -> None:
        self.last_save_msg = msg
        self.last_save_msg_t = time.time()

    def cmd(self, action: str, args: dict | None = None) -> dict:
        args = args or {}
        with self._lock:
            try:
                handler = getattr(self, f"cmd_{action}", None)
                if handler is None:
                    return {"ok": False, "error": f"unknown action: {action}"}
                result = handler(args) or {}
                return {"ok": True, **result}
            except Exception as exc:
                LOGGER.exception("Command %s failed", action)
                return {"ok": False, "error": str(exc)}

    def cmd_start(self, args):
        self._start()

    def cmd_pause(self, args):
        self._pause(auto=False)

    def cmd_resume(self, args):
        self._resume()

    def cmd_stop(self, args):
        if self.screen == self.SCREEN_RUN:
            self._pause(auto=False)
        if self.screen == self.SCREEN_PAUSED:
            self.screen = self.SCREEN_SAVE

    def cmd_save(self, args):
        self._save_and_export()
        self._reset_session()

    def cmd_discard(self, args):
        self.logger.discard()
        self._reset_session()
        self._note("Activite supprimee")

    def cmd_resume_from_save(self, args):
        self._resume()

    def cmd_set_screen(self, args):
        screen = args.get("screen")
        if screen in {self.SCREEN_IDLE, self.SCREEN_CALIB} and self.screen != self.SCREEN_RUN:
            self.screen = screen

    def cmd_set_mode(self, args):
        mode = args.get("mode")
        if mode in {"piste", "random"}:
            self.mode = mode
            self.cfg["route_mode"] = mode
            self._persist_cfg()

    def cmd_toggle_ble(self, args):
        if not self._sensor_available():
            self._note("Feather USB non connecte")
            return
        with data_lock:
            new_state = not sensor_data.get("ble_on", True)
            sensor_data["ble_on"] = new_state
        send_command("BLE_ON" if new_state else "BLE_OFF")

    def cmd_pace_delta(self, args):
        delta = _safe_float(args.get("delta", 0.0), 0.0)
        new_val = round(self.cfg["pace_factor"] + delta, 3)
        self.cfg["pace_factor"] = max(0.5, min(2.0, new_val))
        self._persist_cfg()
        push_param("pace_factor", f"{self.cfg['pace_factor']:.3f}")

    def cmd_incline_delta(self, args):
        delta = _safe_float(args.get("delta", 0.0), 0.0)
        self.cfg["inclin_offset"] = round(self.cfg["inclin_offset"] + delta, 2)
        self._persist_cfg()
        # inclin_offset is the Feather's fallback-only offset; the live incline
        # the Pi pushes each tick already includes it via _apply_incline_points.
        push_param("inclin_offset", f"{self.cfg['inclin_offset']:.2f}")

    def cmd_ref_speed_delta(self, args):
        delta = _safe_float(args.get("delta", 0.0), 0.0)
        self.ref_speed = max(1.0, min(20.0, round(self.ref_speed + delta, 1)))

    def cmd_start_speed_calibration(self, args):
        self.calib_state = "recording"
        self.calib_record_start = time.time()
        self.calib_record_speeds = []

    def cmd_reset_pace(self, args):
        self.cfg["pace_factor"] = 1.0
        self._persist_cfg()
        push_param("pace_factor", "1.000")
        self._note("Facteur vitesse remis a 1.000")

    def cmd_reset_incline(self, args):
        self.cfg["inclin_offset"] = 0.0
        self.cfg["inclin_points"] = {}
        self._persist_cfg()
        send_command("TOF_CAL_CLEAR")
        push_param("inclin_offset", "0.00")
        self._note("Calibration inclinaison effacee")

    def cmd_capture_incline_point(self, args):
        if not self._sensor_available():
            self._note("Capteur inclinaison indisponible")
            return
        target = _safe_float(args.get("target"), math.nan)
        if not math.isfinite(target) or target < -20.0 or target > 20.0:
            raise ValueError("target out of range")
        # Capture the SMOOTHED ToF mm ("inclinaison"), not the instantaneous raw
        # sample — the runtime mapping is fed the smoothed value, so capturing
        # raw would let one noise spike skew a calibration point permanently.
        with data_lock:
            raw = sensor_data.get("inclinaison", 0.0)
        pts = dict(self.cfg.get("inclin_points") or {})
        pts[f"{target:.1f}"] = round(raw, 2)
        self.cfg["inclin_points"] = pts
        self._persist_cfg()
        if abs(target) < 0.01:
            push_param("tof_cal_0_mm", int(round(raw)))
        elif abs(target - 12.0) < 0.01:
            push_param("tof_cal_12_mm", int(round(raw)))
        self._note(f"Point {target:+.0f}% memorise")

    def cmd_clear_incline_points(self, args):
        self.cfg["inclin_points"] = {}
        self._persist_cfg()
        send_command("TOF_CAL_CLEAR")
        self._note("Points effaces")

    def cmd_cad_threshold_delta(self, args):
        delta = _safe_float(args.get("delta", 0.0), 0.0)
        thr = round(_clamp(self.cfg.get("cad_threshold", 2.0) + delta, 0.1, 50.0), 2)
        self.cfg["cad_threshold"] = thr
        # Keep the re-arm level at half the threshold (hysteresis) so the user
        # only has one knob to turn.
        self.cfg["cad_rearm"] = round(thr / 2.0, 2)
        self._persist_cfg()
        push_param("cad_threshold", f"{thr:.2f}")
        push_param("cad_rearm", f"{self.cfg['cad_rearm']:.2f}")

    def cmd_cad_refract_delta(self, args):
        delta = _safe_float(args.get("delta", 0.0), 0.0)
        ms = int(_clamp(self.cfg.get("cad_refract_ms", 180) + delta, 50, 1000))
        self.cfg["cad_refract_ms"] = ms
        self._persist_cfg()
        push_param("cad_refract_ms", ms)

    def cmd_cad_record_start(self, args):
        if self.screen == self.SCREEN_RUN:
            self._note("Arrete la course avant d'enregistrer")
            return {"recording": False}
        if 0 < self.logger.disk_free_mb < 100:
            self._note("Espace disque insuffisant")
            return {"recording": False}
        send_command("CAD_RAW:1")
        path = raw_recorder.start()
        self._note("Enregistrement cadence demarre")
        return {"recording": True, "file": os.path.basename(path or "")}

    def cmd_cad_record_stop(self, args):
        # Leave the $RAW stream ON — the live cadence detector needs it now.
        # Just close the recording file; raw_recorder ignores lines when stopped.
        path, lines = raw_recorder.stop()
        if path:
            self._note(f"Enregistre: {os.path.basename(path)} ({lines} lignes)")
        return {"recording": False, "lines": lines, "file": os.path.basename(path or "")}

    def cmd_feather_diag(self, args):
        queries = ("STATUS", "TOF", "DIAG", "CAD", "CFG_DUMP")
        for query in queries:
            send_command(query)
            time.sleep(0.1)
        self._note("Feather diag voir logs")
        return {"sent": list(queries)}

    def cmd_retry_strava(self, args):
        if not strava_queue:
            return {"pending_count": 0}
        strava_queue.request_drain()
        status = strava_queue.status()
        self._note(f"Retry Strava ({status.get('pending_count', 0)} en attente)")
        return {"pending_count": status.get("pending_count", 0)}


APP = App()


# =====================================================================
# HTTP layer
# =====================================================================

class Handler(BaseHTTPRequestHandler):
    server_version = "TreadmillHTTP/1.0"
    sys_version = ""
    # Drop half-open/idle sockets so a client that connects and sends nothing
    # can't pin a worker thread forever (ThreadingHTTPServer threads accumulate).
    timeout = 30

    def log_message(self, fmt, *args):
        LOGGER.info("%s - %s", self.address_string(), fmt % args)

    def _client_ip(self) -> str:
        return (self.client_address or ("", 0))[0]

    def _client_is_trusted(self) -> bool:
        try:
            ip = ipaddress.ip_address(self._client_ip())
        except ValueError:
            return False
        if ip.is_loopback:
            return True
        return ALLOW_PRIVATE_NETWORKS and (ip.is_private or ip.is_link_local)

    def _has_valid_token(self) -> bool:
        if not API_TOKEN:
            return False
        token = self.headers.get("X-Treadmill-Token", "").strip()
        auth = self.headers.get("Authorization", "").strip()
        # Constant-time compare so a wrong token can't be recovered by timing.
        return (hmac.compare_digest(token, API_TOKEN)
                or hmac.compare_digest(auth, f"Bearer {API_TOKEN}"))

    def _origin_is_allowed(self) -> bool:
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if not origin:
            return True
        host = self.headers.get("Host", "").strip().lower()
        if not host:
            return False
        try:
            return urlparse(origin).netloc.lower() == host
        except Exception:
            return False

    def _authorize_api(self) -> bool:
        return self._client_is_trusted() or self._has_valid_token()

    def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _send_static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        # Guard traversal: require STATIC_DIR + separator as the prefix so a
        # sibling like ".../static_x/secret" (same string prefix) can't escape.
        if (path != STATIC_DIR and not path.startswith(STATIC_DIR + os.sep)) \
                or not os.path.isfile(path):
            self._send(404, b"not found", "text/plain")
            return

        ext = os.path.splitext(path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".jsx": "application/javascript; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".woff2": "font/woff2",
        }.get(ext, "application/octet-stream")

        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            return self._send_static("dashboard.html")
        if parsed.path == "/api/state":
            with data_lock:
                raw = dict(sensor_data)
            return self._send_json(APP.snapshot(raw))
        if parsed.path == "/api/health":
            return self._send_json({"ok": True, "ts": time.time()})
        if parsed.path.startswith("/static/"):
            return self._send_static(parsed.path[len("/static/") :])
        if "/" not in parsed.path[1:]:
            return self._send_static(parsed.path[1:])
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/cmd":
            self._send(404, b"not found", "text/plain")
            return
        if not self._authorize_api():
            self._send_json({"ok": False, "error": "forbidden"}, 403)
            return
        if not self._origin_is_allowed():
            self._send_json({"ok": False, "error": "invalid origin"}, 403)
            return

        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json({"ok": False, "error": "invalid content length"}, 400)
            return
        if size < 0 or size > MAX_REQUEST_BODY_BYTES:
            self._send_json({"ok": False, "error": "request too large"}, 413)
            return

        try:
            body = self.rfile.read(size).decode("utf-8") if size else "{}"
            payload = json.loads(body or "{}")
        except Exception:
            self._send_json({"ok": False, "error": "bad json"}, 400)
            return
        if not isinstance(payload, dict):
            self._send_json({"ok": False, "error": "payload must be an object"}, 400)
            return

        action = payload.get("action", "")
        args = payload.get("args") or {}
        if not isinstance(action, str) or not action or len(action) > 64:
            self._send_json({"ok": False, "error": "invalid action"}, 400)
            return
        if not isinstance(args, dict):
            self._send_json({"ok": False, "error": "args must be an object"}, 400)
            return

        result = APP.cmd(action, args)
        self._send_json(result)


# =====================================================================
# Main
# =====================================================================

def main() -> None:
    global strava_queue

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    # Let the detector read its tunables live from the running config (so a
    # config edit retunes cadence with no restart, no re-flash).
    cadence_detector.bind(lambda: APP.cfg)

    threading.Thread(target=serial_thread_fn, daemon=True).start()

    def app_tick_thread_fn() -> None:
        # Drives recording at a steady 1 Hz regardless of whether any browser
        # is polling. Never let an exception kill it.
        while True:
            try:
                APP.tick()
            except Exception:
                LOGGER.exception("app tick failed")
            time.sleep(1.0)

    threading.Thread(target=app_tick_thread_fn, daemon=True).start()

    if start_hr_monitor:
        try:
            start_hr_monitor(APP.cfg.get("hr_mac") or None)
        except Exception as exc:
            LOGGER.warning("start_hr_monitor failed: %s", exc, exc_info=True)

    if strava_queue:
        try:
            strava_queue.start_drain_loop()
        except Exception as exc:
            LOGGER.warning("strava_queue start failed: %s", exc, exc_info=True)
            strava_queue = None

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    LOGGER.info("server http://%s:%s (static: %s)", args.host, args.port, STATIC_DIR)

    def _shutdown(signum, frame):
        LOGGER.info("received signal %s shutting down", signum)
        try:
            with serial_lock:
                if serial_port and serial_port.is_open:
                    serial_port.close()
        except Exception:
            LOGGER.debug("Serial close during shutdown failed", exc_info=True)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
