#!/usr/bin/env python3
"""
Treadmill Dashboard v8 - HyperPixel 4.0 (800x480)
- Redesigned UI with touch-optimized targets (min 48px)
- Calibration page: sections for speed, inclinaison with toggle/zero
- Flow activite: IDLE -> START -> PAUSE -> SAVE/DISCARD
- Logging cote Pi, compatible firmware v11 (LSM303)
"""

import threading
import time
import json
import os
import csv
import glob
import math
from datetime import datetime
import pygame
import serial

# Import export module (same directory)
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from treadmill_export import export_activity
except ImportError:
    export_activity = None

try:
    from treadmill_strava import upload_tcx, is_configured as strava_configured
except ImportError:
    upload_tcx = None
    strava_configured = lambda: False

try:
    from treadmill_hr import start_hr_monitor, get_hr
except ImportError:
    start_hr_monitor = None
    get_hr = lambda: {"bpm": 0, "connected": False, "device_name": ""}

SCREEN_W, SCREEN_H = 800, 480
FPS = 30
CONFIG_FILE = os.path.expanduser("~/treadmill_config.json")
LOG_DIR = os.path.expanduser("~/treadmill_logs")
LOG_MAX_MB = 500
SERIAL_BAUD = 115200

# ---- PALETTE ----
BG        = (12, 13, 18)
CARD      = (24, 27, 38)
CARD_ALT  = (32, 36, 50)
DIVIDER   = (42, 46, 62)
TXT_DIM   = (75, 80, 95)
TXT_LABEL = (120, 125, 140)
TXT_BODY  = (180, 185, 195)
WHITE     = (232, 236, 242)

GREEN     = (0, 200, 110)
GREEN_DIM = (0, 140, 78)
BLUE      = (45, 130, 240)
BLUE_DIM  = (30, 90, 170)
ORANGE    = (245, 155, 35)
RED       = (230, 55, 55)
RED_DIM   = (160, 40, 40)
YELLOW    = (235, 195, 35)
PURPLE    = (140, 75, 200)

# Button palettes
BTN_GREEN      = (0, 145, 85)
BTN_GREEN_H    = (0, 175, 105)
BTN_RED        = (150, 38, 38)
BTN_RED_H      = (180, 50, 50)
BTN_BLUE       = (35, 100, 190)
BTN_BLUE_H     = (50, 125, 220)
BTN_NEUTRAL    = (42, 48, 65)
BTN_NEUTRAL_H  = (55, 62, 82)
BTN_ORANGE     = (190, 120, 20)
BTN_ORANGE_H   = (220, 145, 35)
BTN_PURPLE     = (100, 55, 150)
BTN_PURPLE_H   = (125, 72, 180)

P_COARSE = (30, 120, 60)
P_MED    = (25, 100, 50)
P_FINE   = (20, 80, 42)
M_COARSE = (130, 35, 35)
M_MED    = (105, 30, 30)
M_FINE   = (80, 25, 25)
P_HOVER  = (45, 160, 85)
M_HOVER  = (165, 50, 50)

# ---- DONNEES ----
sensor_data = {
    "speed_kmh": 0.0, "speed_ms": 0.0, "cadence": 0,
    "distance_m": 0.0, "inclinaison": 0.0, "inclin_raw_smooth": 0.0,
    "pulses": 0, "ble_on": True, "ble_connected": False,
    "serial_connected": False, "last_update": 0
}
data_lock = threading.Lock()
serial_port = None
serial_lock = threading.Lock()


# ==================================
# Display smoother
# ==================================
class DisplaySmoother:
    def __init__(self, alpha=0.25):
        self.alpha = alpha
        self.smooth_speed_kmh = 0.0
        self.smooth_inclin = 0.0
        self.smooth_cadence = 0.0

    def update(self, speed_kmh, inclin, cadence):
        if speed_kmh < 0.1:
            self.smooth_speed_kmh *= 0.5
            if self.smooth_speed_kmh < 0.3:
                self.smooth_speed_kmh = 0.0
            self.smooth_cadence = 0
        else:
            if self.smooth_speed_kmh < 0.1:
                self.smooth_speed_kmh = speed_kmh
                self.smooth_cadence = cadence
            else:
                self.smooth_speed_kmh = self.alpha * speed_kmh + (1 - self.alpha) * self.smooth_speed_kmh
                self.smooth_cadence = self.alpha * cadence + (1 - self.alpha) * self.smooth_cadence
        self.smooth_inclin = self.alpha * inclin + (1 - self.alpha) * self.smooth_inclin

    def get(self):
        return self.smooth_speed_kmh, self.smooth_inclin, int(round(self.smooth_cadence))


# ==================================
# Activity Logger
# ==================================
class ActivityLogger:
    def __init__(self):
        os.makedirs(LOG_DIR, exist_ok=True)
        self.file = None
        self.writer = None
        self.filepath = None
        self.log_interval = 1.0
        self.last_log = 0
        self.log_count = 0
        self.logs_size_mb = 0.0
        self.disk_free_mb = 0.0
        self.refresh_disk_info()

    def refresh_disk_info(self):
        total = 0
        count = 0
        for f in glob.glob(os.path.join(LOG_DIR, "run_*.csv")):
            try:
                total += os.path.getsize(f)
                count += 1
            except: pass
        self.logs_size_mb = total / (1024 * 1024)
        self.log_count = count
        try:
            st = os.statvfs(LOG_DIR)
            self.disk_free_mb = (st.f_bavail * st.f_frsize) / (1024 * 1024)
        except:
            self.disk_free_mb = 0.0
        self._cleanup_if_needed()

    def _cleanup_if_needed(self):
        if self.logs_size_mb <= LOG_MAX_MB:
            return
        files = sorted(glob.glob(os.path.join(LOG_DIR, "run_*.csv")), key=os.path.getmtime)
        removed = 0
        while files and self.logs_size_mb > LOG_MAX_MB * 0.8:
            oldest = files.pop(0)
            try:
                size = os.path.getsize(oldest)
                os.remove(oldest)
                self.logs_size_mb -= size / (1024 * 1024)
                self.log_count -= 1
                removed += 1
            except: pass

    def start(self):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filepath = os.path.join(LOG_DIR, f"run_{ts}.csv")
        self.file = open(self.filepath, "w", newline="")
        self.writer = csv.writer(self.file)
        self.writer.writerow([
            "timestamp", "elapsed_s", "speed_raw_kmh", "speed_smooth_kmh",
            "speed_corr_kmh", "cadence", "inclin_raw", "inclin_corr",
            "distance_m", "pulses", "pace_factor", "heart_rate"
        ])
        self.last_log = 0

    def log(self, elapsed_s, data, config):
        now = time.time()
        if now - self.last_log < self.log_interval:
            return
        self.last_log = now
        if self.writer:
            pf = config["pace_factor"]
            io = config["inclin_offset"]
            hr = get_hr()
            self.writer.writerow([
                datetime.now().strftime("%H:%M:%S"), f"{elapsed_s:.1f}",
                f"{data['speed_kmh']:.2f}", f"{data['speed_ms'] * 3.6:.2f}",
                f"{data['speed_kmh'] * pf:.2f}", data["cadence"],
                f"{data['inclinaison']:.1f}", f"{data['inclinaison'] + io:.1f}",
                f"{data['distance_m']:.2f}", data["pulses"], f"{pf:.3f}",
                hr["bpm"]
            ])

    def save(self):
        if self.file:
            self.file.close()
            self.file = None
            self.writer = None
            self.refresh_disk_info()
            return self.filepath
        return None

    def discard(self):
        path = self.filepath
        if self.file:
            self.file.close()
            self.file = None
            self.writer = None
        if path and os.path.exists(path):
            os.remove(path)
        self.filepath = None
        self.refresh_disk_info()


# ==================================
# Config
# ==================================
def load_config():
    defaults = {"pace_factor": 1.0, "inclin_offset": 0.0,
                "inclin_sign": 1, "inclin_zero": 0.0,
                "calib_speed_ref": 0.0, "calib_speed_raw": 0.0}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                defaults.update(json.load(f))
        except: pass
    return defaults

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


# ==================================
# Serial
# ==================================
def find_serial_port():
    for pattern in ["/dev/ttyACM*", "/dev/ttyUSB*"]:
        ports = glob.glob(pattern)
        if ports: return ports[0]
    return None

def send_command(cmd):
    with serial_lock:
        if serial_port and serial_port.is_open:
            try:
                serial_port.write(f"{cmd}\n".encode())
                serial_port.flush()
            except: pass

def sync_config_to_feather(config):
    send_command(f"PACE_FACTOR:{config['pace_factor']:.3f}")
    time.sleep(0.05)
    send_command(f"INCLIN_OFFSET:{config['inclin_offset']:.2f}")
    time.sleep(0.05)
    send_command(f"INCLIN_SIGN:{config.get('inclin_sign', 1)}")
    time.sleep(0.05)
    send_command(f"INCLIN_ZERO_VAL:{config.get('inclin_zero', 0.0):.2f}")

def parse_data_line(line):
    try:
        parts = line.split(",")
        if len(parts) >= 9 and parts[0] == "$DATA":
            d = {
                "speed_kmh": float(parts[1]), "speed_ms": float(parts[2]),
                "cadence": int(parts[3]), "distance_m": float(parts[4]),
                "inclinaison": float(parts[5]), "pulses": int(parts[6]),
                "ble_on": parts[7] == "1", "ble_connected": parts[8].strip() == "1",
            }
            # Field 9: raw smooth inclinaison (before zero/sign)
            if len(parts) >= 10:
                d["inclin_raw_smooth"] = float(parts[9].strip())
            return d
    except: pass
    return None

def serial_thread_fn():
    global serial_port
    config_synced = False
    while True:
        port_name = find_serial_port()
        if not port_name:
            with data_lock: sensor_data["serial_connected"] = False
            config_synced = False
            time.sleep(2); continue
        try:
            with serial_lock:
                serial_port = serial.Serial(port_name, SERIAL_BAUD, timeout=1)
            with data_lock: sensor_data["serial_connected"] = True
            if not config_synced:
                time.sleep(1)
                config = load_config()
                sync_config_to_feather(config)
                config_synced = True
            while True:
                with serial_lock:
                    if not serial_port or not serial_port.is_open: break
                    try: line = serial_port.readline().decode("utf-8", errors="ignore").strip()
                    except: break
                if not line: continue
                parsed = parse_data_line(line)
                if parsed:
                    with data_lock:
                        sensor_data.update(parsed)
                        sensor_data["serial_connected"] = True
                        sensor_data["last_update"] = time.time()
        except: pass
        with serial_lock:
            if serial_port:
                try: serial_port.close()
                except: pass
                serial_port = None
        with data_lock: sensor_data["serial_connected"] = False
        config_synced = False
        time.sleep(2)


# ==================================
# UI Helpers
# ==================================
def rounded_rect(surf, color, rect, r=10):
    x, y, w, h = rect
    r = min(r, h // 2, w // 2)
    pygame.draw.rect(surf, color, (x + r, y, w - 2 * r, h))
    pygame.draw.rect(surf, color, (x, y + r, w, h - 2 * r))
    for cx, cy in [(x + r, y + r), (x + w - r, y + r), (x + r, y + h - r), (x + w - r, y + h - r)]:
        pygame.draw.circle(surf, color, (cx, cy), r)


class Button:
    def __init__(self, x, y, w, h, label, color, hover, text_color=WHITE, font_key="md"):
        self.rect = pygame.Rect(x, y, w, h)
        self.label = label
        self.color = color
        self.hover = hover
        self.text_color = text_color
        self.font_key = font_key
        self.pressed = False
        self.press_time = 0
        self.visible = True

    def draw(self, surface, fonts):
        if not self.visible: return
        active = self.pressed and (time.time() - self.press_time < 0.12)
        c = self.hover if active else self.color
        rounded_rect(surface, c, self.rect, 8)
        t = fonts[self.font_key].render(self.label, True, self.text_color)
        surface.blit(t, t.get_rect(center=self.rect.center))

    def handle_event(self, event):
        if not self.visible: return False
        if event.type == pygame.MOUSEBUTTONDOWN and self.rect.collidepoint(event.pos):
            self.pressed = True
            self.press_time = time.time()
            return True
        if event.type == pygame.MOUSEBUTTONUP:
            self.pressed = False
        return False


class Toggle:
    """Touch-friendly toggle switch (persisted in config)"""
    def __init__(self, x, y, w=70, h=36, label="", on_color=GREEN, off_color=DIVIDER):
        self.rect = pygame.Rect(x, y, w, h)
        self.label = label
        self.on_color = on_color
        self.off_color = off_color
        self.is_on = False

    def draw(self, surface, fonts):
        r = self.rect.height // 2
        bg = self.on_color if self.is_on else self.off_color
        rounded_rect(surface, bg, self.rect, r)
        # Knob
        knob_r = r - 4
        if self.is_on:
            cx = self.rect.right - r
        else:
            cx = self.rect.left + r
        pygame.draw.circle(surface, WHITE, (cx, self.rect.centery), knob_r)
        if self.label:
            t = fonts["xs"].render(self.label, True, TXT_LABEL)
            surface.blit(t, (self.rect.right + 10, self.rect.centery - t.get_height() // 2))

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN and self.rect.collidepoint(event.pos):
            self.is_on = not self.is_on
            return True
        return False


class ToggleButton(Button):
    def __init__(self, x, y, w, h, label_on, label_off, color_on, color_off, **kw):
        super().__init__(x, y, w, h, label_on, color_on, color_on, **kw)
        self.label_on = label_on
        self.label_off = label_off
        self.color_on = color_on
        self.color_off = color_off
        self.is_on = True

    def draw(self, surface, fonts):
        self.color = self.color_on if self.is_on else self.color_off
        self.label = self.label_on if self.is_on else self.label_off
        super().draw(surface, fonts)

    def handle_event(self, event):
        if event.type == pygame.MOUSEBUTTONDOWN and self.rect.collidepoint(event.pos):
            self.is_on = not self.is_on
            self.press_time = time.time()
            self.pressed = True
            return True
        if event.type == pygame.MOUSEBUTTONUP:
            self.pressed = False
        return False


def make_adj_buttons(x, y, bw, bh, gap):
    return {
        "p100": Button(x,              y,        bw, bh, "+1",   P_COARSE, P_HOVER, font_key="lg"),
        "p010": Button(x+bw+gap,       y,        bw, bh, "+.1",  P_MED,    P_HOVER, font_key="lg"),
        "p001": Button(x+2*(bw+gap),   y,        bw, bh, "+.01", P_FINE,   P_HOVER, font_key="md"),
        "m001": Button(x,              y+bh+gap, bw, bh, "-.01", M_FINE,   M_HOVER, font_key="md"),
        "m010": Button(x+bw+gap,       y+bh+gap, bw, bh, "-.1",  M_MED,    M_HOVER, font_key="lg"),
        "m100": Button(x+2*(bw+gap),   y+bh+gap, bw, bh, "-1",   M_COARSE, M_HOVER, font_key="lg"),
    }


# ==================================
# MAIN PAGE
# ==================================
class MainPage:
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"

    def __init__(self, config):
        self.config = config

        # Pace: 6 boutons en 2x3, plus gros, etendus vers la gauche
        pbw, pbh, pgap = 100, 56, 8
        px = 800 - 3 * (pbw + pgap) + pgap - 10
        self.pace_btns = {
            "p001": Button(px,              12, pbw, pbh, "+.01", P_FINE, P_HOVER, font_key="lg"),
            "p010": Button(px+pbw+pgap,     12, pbw, pbh, "+.1",  P_MED, P_HOVER, font_key="lg"),
            "p100": Button(px+2*(pbw+pgap), 12, pbw, pbh, "+1",   P_COARSE, P_HOVER, font_key="lg"),
            "m001": Button(px,              12+pbh+pgap, pbw, pbh, "-.01", M_FINE, M_HOVER, font_key="lg"),
            "m010": Button(px+pbw+pgap,     12+pbh+pgap, pbw, pbh, "-.1",  M_MED, M_HOVER, font_key="lg"),
            "m100": Button(px+2*(pbw+pgap), 12+pbh+pgap, pbw, pbh, "-1",   M_COARSE, M_HOVER, font_key="lg"),
        }

        # Inclinaison: 4 boutons (-1, -0.5, +0.5, +1)
        ibw, ibh, igap = 120, 56, 8
        ix = 800 - 2 * (ibw + igap) + igap - 10
        self.incl_btns = {
            "p05": Button(ix,          152, ibw, ibh, "+0.5", P_MED, P_HOVER, font_key="lg"),
            "p10": Button(ix+ibw+igap, 152, ibw, ibh, "+1",   P_COARSE, P_HOVER, font_key="lg"),
            "m05": Button(ix,          152+ibh+igap, ibw, ibh, "-0.5", M_MED, M_HOVER, font_key="lg"),
            "m10": Button(ix+ibw+igap, 152+ibh+igap, ibw, ibh, "-1",   M_COARSE, M_HOVER, font_key="lg"),
        }

        ay, abh = 300, 52
        self.btn_start   = Button(15, ay, 185, abh, "START",   BTN_GREEN, BTN_GREEN_H, font_key="lg")
        self.btn_pause   = Button(15, ay, 185, abh, "PAUSE",   BTN_ORANGE, BTN_ORANGE_H, font_key="lg")
        self.btn_resume  = Button(15, ay, 130, abh, "RESUME",  BTN_BLUE, BTN_BLUE_H, font_key="md")
        self.btn_save    = Button(155, ay, 120, abh, "SAVE",    BTN_GREEN, BTN_GREEN_H, font_key="md")
        self.btn_discard = Button(285, ay, 120, abh, "ANNULER", BTN_RED, BTN_RED_H, font_key="md")
        self.btn_ble     = ToggleButton(420, ay, 170, abh, "BLE: ON", "BLE: OFF",
                                         GREEN_DIM, RED_DIM, font_key="md")
        self.btn_calib   = Button(600, ay, 185, abh, "CALIBRATION", BTN_NEUTRAL, BTN_NEUTRAL_H, font_key="md")

        self.activity_state = self.IDLE
        self.session_start = None
        self.elapsed_at_pause = 0.0
        self.distance_offset = 0.0
        self.smoother = DisplaySmoother(alpha=0.25)
        self.logger = ActivityLogger()
        self.saved_msg = ""
        self.saved_msg_time = 0
        # Elevation tracking
        self.elev_gain = 0.0
        self.elev_loss = 0.0
        self.prev_distance = 0.0
        # Auto-pause
        self.auto_pause = True  # Feature toggle
        self.speed_zero_since = 0  # Timestamp when speed dropped to 0
        self.auto_paused = False   # True if current pause was triggered by auto-pause
        self.AUTO_PAUSE_DELAY = 5  # Seconds of 0 speed before auto-pause

    def handle_event(self, event):
        pace_deltas = {"p001": 0.01, "p010": 0.1, "p100": 1.0, "m001": -0.01, "m010": -0.1, "m100": -1.0}
        for name, btn in self.pace_btns.items():
            if btn.handle_event(event):
                self.config["pace_factor"] = max(0.1, round(self.config["pace_factor"] + pace_deltas[name], 3))
                save_config(self.config)
                sync_config_to_feather(self.config)
        incl_deltas = {"p05": 0.5, "p10": 1.0, "m05": -0.5, "m10": -1.0}
        for name, btn in self.incl_btns.items():
            if btn.handle_event(event):
                self.config["inclin_offset"] = round(self.config["inclin_offset"] + incl_deltas[name], 1)
                save_config(self.config)
                sync_config_to_feather(self.config)

        if self.activity_state == self.IDLE:
            if self.btn_start.handle_event(event):
                self.activity_state = self.RUNNING
                self.session_start = time.time()
                self.elapsed_at_pause = 0.0
                self.distance_offset = 0.0  # Feather reset a 0 via RESET_DIST
                self.elev_gain = 0.0
                self.elev_loss = 0.0
                self.prev_distance = 0.0
                send_command("RESET_DIST")
                self.logger.start()
        elif self.activity_state == self.RUNNING:
            if self.btn_pause.handle_event(event):
                self.activity_state = self.PAUSED
                self.elapsed_at_pause += time.time() - self.session_start
                self.auto_paused = False  # Manual pause, don't auto-resume
        elif self.activity_state == self.PAUSED:
            if self.btn_resume.handle_event(event):
                self.activity_state = self.RUNNING
                self.session_start = time.time()
            elif self.btn_save.handle_event(event):
                path = self.logger.save()
                self.activity_state = self.IDLE
                if path:
                    # Auto-export TCX pour Strava
                    if export_activity:
                        tcx_path, result = export_activity(path)
                        if tcx_path and isinstance(result, dict):
                            gain = result["elevation_gain"]
                            loss = result["elevation_loss"]
                            dist = result["distance_km"]
                            pace = result["avg_pace"]
                            self.saved_msg = f"TCX: {dist:.2f}km | D+{gain:.0f}m D-{loss:.0f}m | {pace}"
                            # Upload Strava en background (non-bloquant)
                            if upload_tcx and strava_configured():
                                def _upload():
                                    ok, msg = upload_tcx(tcx_path)
                                    self.saved_msg = f"Strava: {msg}" if ok else f"Strava err: {msg}"
                                    self.saved_msg_time = time.time()
                                threading.Thread(target=_upload, daemon=True).start()
                        else:
                            self.saved_msg = f"CSV OK, TCX erreur"
                    else:
                        self.saved_msg = f"Sauvegarde: {os.path.basename(path)}"
                    self.saved_msg_time = time.time()
            elif self.btn_discard.handle_event(event):
                self.logger.discard()
                self.activity_state = self.IDLE
                self.saved_msg = "Activite annulee"
                self.saved_msg_time = time.time()

        if self.btn_ble.handle_event(event):
            send_command("BLE_ON" if self.btn_ble.is_on else "BLE_OFF")
        if self.btn_calib.handle_event(event):
            return "calibration"
        return None

    def get_elapsed(self):
        if self.activity_state == self.RUNNING and self.session_start:
            return self.elapsed_at_pause + (time.time() - self.session_start)
        return self.elapsed_at_pause

    def draw(self, surface, fonts, data):
        raw_speed_kmh = data["speed_kmh"] * self.config["pace_factor"]
        raw_inclin = data["inclinaison"] + self.config["inclin_offset"]
        raw_cadence = data["cadence"]
        self.smoother.update(raw_speed_kmh, raw_inclin, raw_cadence)
        speed_kmh, inclin, cadence = self.smoother.get()
        distance_m = max(0, (data["distance_m"] - self.distance_offset) * self.config["pace_factor"])

        # Auto-pause logic
        if self.auto_pause:
            now = time.time()
            if speed_kmh < 0.2:
                if self.speed_zero_since == 0:
                    self.speed_zero_since = now
                elif (self.activity_state == self.RUNNING and
                      now - self.speed_zero_since >= self.AUTO_PAUSE_DELAY):
                    # Auto-pause
                    self.activity_state = self.PAUSED
                    self.elapsed_at_pause += now - self.session_start
                    self.auto_paused = True
            else:
                self.speed_zero_since = 0
                if self.activity_state == self.PAUSED and self.auto_paused:
                    # Auto-resume
                    self.activity_state = self.RUNNING
                    self.session_start = time.time()
                    self.auto_paused = False

        elapsed = self.get_elapsed()

        if self.activity_state == self.RUNNING:
            self.logger.log(elapsed, data, self.config)
            # Calculer elevation live
            cur_dist = distance_m
            delta_dist = cur_dist - self.prev_distance
            self.prev_distance = cur_dist
            if delta_dist > 0 and abs(inclin) > 0.1:
                delta_alt = delta_dist * math.sin(math.radians(inclin))
                if delta_alt > 0:
                    self.elev_gain += delta_alt
                else:
                    self.elev_loss += abs(delta_alt)

        pace_str = f"{int(3600 / speed_kmh // 60)}:{int(3600 / speed_kmh % 60):02d}" if speed_kmh > 0.3 else "--:--"
        dstr, dunit = (f"{distance_m / 1000:.2f}", "km") if distance_m >= 1000 else (f"{distance_m:.0f}", "m")
        estr = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"

        # ---- PACE CARD ----
        rounded_rect(surface, CARD, (10, 8, 780, 128), 12)
        surface.blit(fonts["xs"].render("PACE", True, TXT_DIM), (22, 14))
        if self.config["pace_factor"] != 1.0:
            surface.blit(fonts["xs"].render(f"x{self.config['pace_factor']:.2f}", True, ORANGE), (65, 14))
        val = fonts["huge"].render(pace_str, True, GREEN)
        surface.blit(val, (22, 32))
        surface.blit(fonts["md"].render("min/km", True, TXT_DIM), (22 + val.get_width() + 8, 85))
        for btn in self.pace_btns.values(): btn.draw(surface, fonts)

        # ---- INCLINAISON CARD ----
        rounded_rect(surface, CARD, (10, 146, 780, 128), 12)
        surface.blit(fonts["xs"].render("INCLINAISON", True, TXT_DIM), (22, 152))
        if self.config["inclin_offset"] != 0:
            surface.blit(fonts["xs"].render(f"offset {self.config['inclin_offset']:+.2f}", True, ORANGE), (120, 152))
        sign_icon = "" if self.config.get("inclin_sign", 1) == 1 else " [INV]"
        val = fonts["huge"].render(f"{inclin:+.1f}", True, BLUE)
        surface.blit(val, (22, 170))
        surface.blit(fonts["md"].render(f"deg{sign_icon}", True, TXT_DIM), (22 + val.get_width() + 8, 222))
        for btn in self.incl_btns.values(): btn.draw(surface, fonts)

        # ---- ACTION BUTTONS ----
        if self.activity_state == self.IDLE:
            self.btn_start.draw(surface, fonts)
        elif self.activity_state == self.RUNNING:
            self.btn_pause.draw(surface, fonts)
        elif self.activity_state == self.PAUSED:
            self.btn_resume.draw(surface, fonts)
            self.btn_save.draw(surface, fonts)
            self.btn_discard.draw(surface, fonts)

        self.btn_ble.is_on = data.get("ble_on", True)
        self.btn_ble.draw(surface, fonts)
        self.btn_calib.draw(surface, fonts)

        # ---- METRICS ROW ----
        my, mh, mgap = 368, 75, 5
        mw = (780 - 5 * mgap) // 6  # 6 metrics
        elev_str = f"{self.elev_gain:.0f}" if self.elev_gain < 1000 else f"{self.elev_gain / 1000:.1f}k"
        hr = get_hr()
        hr_str = f"{hr['bpm']}" if hr["bpm"] > 0 else "--"
        hr_color = RED if hr["bpm"] > 160 else (ORANGE if hr["bpm"] > 140 else (GREEN if hr["bpm"] > 0 else TXT_DIM))
        metrics = [
            ("DISTANCE", dstr, dunit, WHITE),
            ("TEMPS", estr, "", WHITE),
            ("CADENCE", f"{cadence}", "spm", WHITE),
            ("VITESSE", f"{speed_kmh:.1f}", "km/h", WHITE),
            ("D+", elev_str, "m", GREEN if self.elev_gain > 0 else TXT_DIM),
            ("HR", hr_str, "bpm", hr_color),
        ]
        for i, (label, value, unit_str, val_color) in enumerate(metrics):
            x = 10 + i * (mw + mgap)
            rounded_rect(surface, CARD, (x, my, mw, mh), 10)
            surface.blit(fonts["xs"].render(label, True, TXT_DIM), (x + 10, my + 6))
            v = fonts["xl"].render(value, True, val_color)
            surface.blit(v, (x + 10, my + 24))
            if unit_str:
                surface.blit(fonts["sm"].render(unit_str, True, TXT_DIM), (x + 12 + v.get_width(), my + 42))

        # ---- STATUS BAR ----
        sy = 454
        # USB
        uc = GREEN if data["serial_connected"] else RED
        pygame.draw.circle(surface, uc, (16, sy + 7), 4)
        surface.blit(fonts["xs"].render("USB", True, uc), (25, sy))
        # BLE
        if data.get("ble_connected"):   bc, bt = GREEN, "BLE"
        elif data.get("ble_on"):        bc, bt = ORANGE, "BLE..."
        else:                           bc, bt = RED, "BLE OFF"
        pygame.draw.circle(surface, bc, (78, sy + 7), 4)
        surface.blit(fonts["xs"].render(bt, True, bc), (87, sy))
        # HR
        hrc = GREEN if hr["connected"] else TXT_DIM
        hrt = f"HR:{hr['bpm']}" if hr["connected"] else "HR:--"
        pygame.draw.circle(surface, hrc, (138, sy + 7), 4)
        surface.blit(fonts["xs"].render(hrt, True, hrc), (147, sy))
        # Activity state
        if self.activity_state == self.RUNNING:
            rc = RED if int(time.time() * 2) % 2 == 0 else BG
            pygame.draw.circle(surface, rc, (200, sy + 7), 5)
            surface.blit(fonts["xs"].render("REC", True, RED), (209, sy))
        elif self.activity_state == self.PAUSED:
            lbl = "AUTO-PAUSE" if self.auto_paused else "PAUSE"
            surface.blit(fonts["xs"].render(lbl, True, YELLOW), (197, sy))
        else:
            surface.blit(fonts["xs"].render("IDLE", True, TXT_DIM), (197, sy))
        # Disk
        lg = self.logger
        sz = f"{lg.logs_size_mb:.0f}MB" if lg.logs_size_mb >= 1 else f"{lg.logs_size_mb * 1024:.0f}KB"
        pct = (lg.logs_size_mb / LOG_MAX_MB * 100) if LOG_MAX_MB > 0 else 0
        dc = GREEN if pct < 70 else (ORANGE if pct < 90 else RED)
        surface.blit(fonts["xs"].render(f"{lg.log_count} runs | {sz}/{LOG_MAX_MB}MB | Pi: {lg.disk_free_mb:.0f}MB", True, dc), (260, sy))
        # Saved msg (8s for Strava results)
        if self.saved_msg and time.time() - self.saved_msg_time < 8.0:
            msg_color = GREEN if "Strava: OK" in self.saved_msg or "TCX:" in self.saved_msg else ORANGE
            surface.blit(fonts["sm"].render(self.saved_msg, True, msg_color), (260, sy - 18))


# ==================================
# CALIBRATION PAGE
# ==================================
class CalibrationPage:
    def __init__(self, config):
        self.config = config
        self.state = "idle"
        self.record_start = 0
        self.record_speeds = []
        self.ref_speed = 5.0

        # Top bar
        self.btn_back = Button(15, 12, 140, 50, "< RETOUR", BTN_NEUTRAL, BTN_NEUTRAL_H, font_key="md")

        # --- Speed calibration section (left) ---
        self.btn_ref_up = Button(260, 100, 70, 55, "+", P_COARSE, P_HOVER, font_key="xl")
        self.btn_ref_dn = Button(340, 100, 70, 55, "-", M_COARSE, M_HOVER, font_key="xl")
        self.btn_start_calib = Button(30, 175, 280, 55, "CALIBRER VITESSE", BTN_GREEN, BTN_GREEN_H, font_key="lg")
        self.btn_cancel = Button(30, 175, 280, 55, "ANNULER", BTN_RED, BTN_RED_H, font_key="lg")
        self.btn_reset_pace = Button(320, 175, 100, 55, "RESET", BTN_ORANGE, BTN_ORANGE_H, font_key="md")

        # --- Inclinaison section (right) ---
        self.btn_zero_incl = Button(450, 100, 160, 55, "ZERO INCL", BTN_BLUE, BTN_BLUE_H, font_key="md")
        self.btn_reset_incl = Button(620, 100, 160, 55, "RESET OFFSET", BTN_ORANGE, BTN_ORANGE_H, font_key="md")
        self.toggle_invert = Toggle(450, 180, 65, 34, "Inverser inclinaison", on_color=PURPLE)
        self.toggle_invert.is_on = (config.get("inclin_sign", 1) == -1)

        # --- Config display (bottom) ---
        # No buttons, just display

    def handle_event(self, event, data):
        if self.btn_back.handle_event(event):
            self.state = "idle"
            return "main"

        if self.state == "idle":
            if self.btn_start_calib.handle_event(event):
                self.state = "recording"
                self.record_start = time.time()
                self.record_speeds = []
            if self.btn_ref_up.handle_event(event):
                self.ref_speed = min(20.0, self.ref_speed + 0.5)
            if self.btn_ref_dn.handle_event(event):
                self.ref_speed = max(1.0, self.ref_speed - 0.5)
            if self.btn_reset_pace.handle_event(event):
                self.config["pace_factor"] = 1.0
                save_config(self.config)
                sync_config_to_feather(self.config)
            if self.btn_zero_incl.handle_event(event):
                send_command("INCLIN_ZERO")
                # Save the RAW smooth value (before zero/sign) for persistence
                self.config["inclin_zero"] = data.get("inclin_raw_smooth", data.get("inclinaison", 0.0))
                save_config(self.config)
            if self.btn_reset_incl.handle_event(event):
                self.config["inclin_offset"] = 0.0
                save_config(self.config)
                sync_config_to_feather(self.config)
            if self.toggle_invert.handle_event(event):
                self.config["inclin_sign"] = -1 if self.toggle_invert.is_on else 1
                save_config(self.config)
                send_command("INCLIN_INVERT")
        elif self.state == "recording":
            if self.btn_cancel.handle_event(event):
                self.state = "idle"
        return None

    def draw(self, surface, fonts, data):
        # Header
        self.btn_back.draw(surface, fonts)
        surface.blit(fonts["xl"].render("CALIBRATION", True, WHITE), (175, 16))

        if self.state == "idle":
            # ---- DIVIDER LINE ----
            pygame.draw.line(surface, DIVIDER, (430, 80), (430, 240), 1)

            # ==== LEFT: SPEED CALIBRATION ====
            surface.blit(fonts["sm"].render("VITESSE", True, GREEN), (30, 72))
            pygame.draw.line(surface, GREEN_DIM, (30, 90), (100, 90), 2)

            # Reference speed
            surface.blit(fonts["xs"].render("REFERENCE:", True, TXT_DIM), (30, 105))
            surface.blit(fonts["xl"].render(f"{self.ref_speed:.1f}", True, WHITE), (140, 98))
            surface.blit(fonts["sm"].render("km/h", True, TXT_DIM), (215, 110))
            self.btn_ref_up.draw(surface, fonts)
            self.btn_ref_dn.draw(surface, fonts)

            # Current raw speed
            surface.blit(fonts["xs"].render("BRUTE:", True, TXT_DIM), (30, 150))
            surface.blit(fonts["md"].render(f"{data['speed_kmh']:.1f} km/h", True, TXT_BODY), (95, 147))

            self.btn_start_calib.draw(surface, fonts)
            self.btn_reset_pace.draw(surface, fonts)

            # ==== RIGHT: INCLINAISON ====
            surface.blit(fonts["sm"].render("INCLINAISON", True, BLUE), (450, 72))
            pygame.draw.line(surface, BLUE_DIM, (450, 90), (545, 90), 2)

            self.btn_zero_incl.draw(surface, fonts)
            self.btn_reset_incl.draw(surface, fonts)

            # Toggle invert
            self.toggle_invert.draw(surface, fonts)

            # Current inclinaison
            surface.blit(fonts["xs"].render("ACTUELLE:", True, TXT_DIM), (450, 230))
            surface.blit(fonts["xl"].render(f"{data['inclinaison']:+.1f}", True, BLUE), (545, 222))
            surface.blit(fonts["sm"].render("deg", True, TXT_DIM), (660, 234))

            # ==== BOTTOM: CONFIG SUMMARY ====
            cy = 280
            rounded_rect(surface, CARD, (15, cy, 770, 100), 10)
            surface.blit(fonts["sm"].render("CONFIGURATION ACTUELLE", True, TXT_DIM), (30, cy + 8))
            pygame.draw.line(surface, DIVIDER, (30, cy + 28), (770, cy + 28), 1)

            col1_x, col2_x, col3_x = 30, 290, 550
            vy = cy + 38

            surface.blit(fonts["md"].render(f"Pace: x{self.config['pace_factor']:.3f}", True, WHITE), (col1_x, vy))
            if self.config["calib_speed_ref"] > 0:
                surface.blit(fonts["xs"].render(
                    f"({self.config['calib_speed_raw']:.1f} -> {self.config['calib_speed_ref']:.1f})",
                    True, TXT_DIM), (col1_x, vy + 28))

            surface.blit(fonts["md"].render(f"Offset: {self.config['inclin_offset']:+.2f} deg", True, WHITE), (col2_x, vy))
            surface.blit(fonts["xs"].render(f"Zero: {self.config.get('inclin_zero', 0):.1f}", True, TXT_DIM), (col2_x, vy + 28))

            sign_label = "Inverse" if self.config.get("inclin_sign", 1) == -1 else "Normal"
            sign_color = PURPLE if self.config.get("inclin_sign", 1) == -1 else GREEN
            surface.blit(fonts["md"].render(f"Direction: {sign_label}", True, sign_color), (col3_x, vy))

            # Disk info
            lg_count = len(glob.glob(os.path.join(LOG_DIR, "run_*.csv")))
            surface.blit(fonts["xs"].render(f"{lg_count} activites enregistrees", True, TXT_DIM), (col3_x, vy + 28))

        elif self.state == "recording":
            elapsed = time.time() - self.record_start
            remaining = max(0, 10 - elapsed)
            progress = min(elapsed / 10.0, 1.0)

            surface.blit(fonts["lg"].render(f"Calibration... {remaining:.0f}s", True, RED), (50, 95))

            # Progress bar
            rounded_rect(surface, DIVIDER, (50, 145, 700, 30), 8)
            if progress > 0.01:
                rounded_rect(surface, GREEN, (50, 145, int(700 * progress), 30), 8)
            surface.blit(fonts["sm"].render(f"{int(progress * 100)}%", True, WHITE), (370, 148))

            # Data
            surface.blit(fonts["lg"].render(f"Brut: {data['speed_kmh']:.1f} km/h", True, WHITE), (50, 200))
            surface.blit(fonts["lg"].render(f"Ref:  {self.ref_speed:.1f} km/h", True, GREEN), (50, 240))
            surface.blit(fonts["md"].render(f"Echantillons: {len(self.record_speeds)}", True, TXT_DIM), (50, 285))

            self.btn_cancel.draw(surface, fonts)

        # ==== BOTTOM STATUS (always visible) ====
        sy = 400
        rounded_rect(surface, CARD_ALT, (15, sy, 770, 68), 10)
        surface.blit(fonts["xs"].render("DONNEES LIVE", True, TXT_DIM), (30, sy + 6))
        pygame.draw.line(surface, DIVIDER, (30, sy + 22), (770, sy + 22), 1)

        live_y = sy + 30
        items = [
            ("Vitesse", f"{data['speed_kmh']:.1f} km/h", WHITE),
            ("Inclin", f"{data['inclinaison']:+.1f} deg", BLUE),
            ("Cadence", f"{data['cadence']} spm", WHITE),
            ("BLE", "ON" if data.get("ble_connected") else "OFF", GREEN if data.get("ble_connected") else RED),
            ("USB", "OK" if data.get("serial_connected") else "---", GREEN if data.get("serial_connected") else RED),
        ]
        ix = 30
        for label, val, col in items:
            surface.blit(fonts["xs"].render(label, True, TXT_DIM), (ix, live_y))
            surface.blit(fonts["md"].render(val, True, col), (ix, live_y + 14))
            ix += 155


# ==================================
# MAIN
# ==================================
def main():
    pygame.init()
    screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), pygame.FULLSCREEN)
    pygame.display.set_caption("Treadmill Dashboard")
    pygame.mouse.set_visible(False)
    clock = pygame.time.Clock()

    fonts = {
        "xs":   pygame.font.SysFont("DejaVu Sans", 14),
        "sm":   pygame.font.SysFont("DejaVu Sans", 17),
        "md":   pygame.font.SysFont("DejaVu Sans", 22),
        "lg":   pygame.font.SysFont("DejaVu Sans", 28),
        "xl":   pygame.font.SysFont("DejaVu Sans", 38),
        "xxl":  pygame.font.SysFont("DejaVu Sans", 52),
        "huge": pygame.font.SysFont("DejaVu Sans", 68),
    }

    config = load_config()
    main_page = MainPage(config)
    calib_page = CalibrationPage(config)
    current_page = "main"

    threading.Thread(target=serial_thread_fn, daemon=True).start()
    if start_hr_monitor:
        start_hr_monitor()

    running = True
    while running:
        with data_lock: data = dict(sensor_data)
        for event in pygame.event.get():
            if event.type == pygame.QUIT: running = False
            if event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False
            if current_page == "main":
                r = main_page.handle_event(event)
                if r == "calibration": current_page = "calibration"
            elif current_page == "calibration":
                r = calib_page.handle_event(event, data)
                if r == "main": current_page = "main"

        if current_page == "calibration" and calib_page.state == "recording":
            if data["speed_kmh"] > 0.1:
                calib_page.record_speeds.append(data["speed_kmh"])
            if time.time() - calib_page.record_start >= 10:
                if len(calib_page.record_speeds) > 3:
                    avg = sum(calib_page.record_speeds) / len(calib_page.record_speeds)
                    if avg > 0.1:
                        config["pace_factor"] = calib_page.ref_speed / avg
                        config["calib_speed_ref"] = calib_page.ref_speed
                        config["calib_speed_raw"] = avg
                        save_config(config)
                        sync_config_to_feather(config)
                calib_page.state = "idle"

        screen.fill(BG)
        if current_page == "main": main_page.draw(screen, fonts, data)
        else: calib_page.draw(screen, fonts, data)
        pygame.display.flip()
        clock.tick(FPS)

    if main_page.activity_state == main_page.RUNNING:
        path = main_page.logger.save()
        if path and export_activity:
            export_activity(path)
    pygame.quit()

if __name__ == "__main__":
    main()
