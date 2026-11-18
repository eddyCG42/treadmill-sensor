#!/bin/bash
# ----------------------------------------------------------------------
# Treadmill Dashboard v12 — one-shot deployment on the Pi.
#
# Run this AFTER you have copied the web_dashboard/ folder to
# ~/treadmill_dashboard/web_dashboard/.
#
#   bash ~/treadmill_dashboard/web_dashboard/deploy.sh
#
# What it does:
#   1. Verifies layout + that helper modules are reachable
#   2. Installs system packages (chromium, unclutter, curl, python3-serial)
#   3. Runs setup.sh to fetch React + Babel into static/vendor/
#   4. Removes the legacy pygame service if present (avoids serial conflict)
#   5. Installs the systemd unit + reloads + enables --now
#   6. Smoke-tests /api/health
#   7. Installs the Chromium kiosk autostart
#
# Idempotent: re-running is safe.
# ----------------------------------------------------------------------

set -euo pipefail

USER_NAME="${USER:-eddycg}"
HOME_DIR="$HOME"
ROOT="$HOME_DIR/treadmill_dashboard"
WEB="$ROOT/web_dashboard"
SERVICE_SRC="$WEB/kiosk/treadmill-server.service"
SERVICE_DST="/etc/systemd/system/treadmill-server.service"
KIOSK_DESKTOP_SRC="$WEB/kiosk/treadmill-kiosk.desktop"
KIOSK_DESKTOP_DST="$HOME_DIR/.config/autostart/treadmill-kiosk.desktop"
KIOSK_USER_UNIT_SRC="$WEB/kiosk/treadmill-kiosk.service"
KIOSK_USER_UNIT_DST="$HOME_DIR/.config/systemd/user/treadmill-kiosk.service"
PYTHON_BIN="$(command -v python3 || true)"

if [ -z "$PYTHON_BIN" ]; then
  echo "ERROR: python3 not found in PATH."
  exit 1
fi

# NETWORK BIND POLICY:
# Current: --host 0.0.0.0 (open on LAN, no auth — convenient but insecure).
# Audit recommendation: bind 127.0.0.1 + reverse proxy with basic auth.
# To harden: drop `--host 0.0.0.0` (server defaults to 127.0.0.1) and set up
# nginx/caddy on the Pi with basic auth as the public-facing entry.
create_server_unit() {
  cat <<EOF
[Unit]
Description=Treadmill Dashboard HTTP Server
After=network-online.target bluetooth.service
Wants=network-online.target bluetooth.service

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$WEB
ExecStart=$PYTHON_BIN $WEB/treadmill_server.py --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=3
SupplementaryGroups=dialout bluetooth
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$HOME_DIR $HOME_DIR/treadmill_logs $HOME_DIR/treadmill_exports $HOME_DIR/.cache
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
}

echo "===================================================================="
echo " Treadmill Dashboard v12 deployment — user=$USER_NAME"
echo "===================================================================="

# -------- 1. Sanity --------
echo "[1/7] Checking layout…"
[ -d "$WEB" ] || { echo "  ERROR: $WEB not found. Copy web_dashboard/ there first."; exit 1; }
[ -f "$WEB/treadmill_server.py" ] || { echo "  ERROR: treadmill_server.py missing."; exit 1; }
for m in treadmill_metrics.py treadmill_cadence.py treadmill_export.py treadmill_strava.py treadmill_hr.py; do
  if [ ! -f "$ROOT/$m" ]; then
    echo "  WARN: $ROOT/$m missing — features depending on it will be disabled."
  else
    echo "  OK    $m"
  fi
done

# -------- 2. APT packages --------
echo "[2/7] Installing system packages (sudo password may be requested)…"
sudo apt update -qq
# Try chromium first (Bookworm/Trixie+), fall back to chromium-browser (older).
CHROMIUM_PKG=""
for pkg in chromium chromium-browser; do
  if sudo apt install -y -qq "$pkg" 2>/dev/null; then
    CHROMIUM_PKG="$pkg"
    break
  fi
done
if [ -z "$CHROMIUM_PKG" ]; then
  echo "  ERROR: could not install chromium (tried: chromium, chromium-browser)."
  echo "         Install one of them manually, then re-run this script."
  exit 1
fi
echo "  → installed $CHROMIUM_PKG"
sudo apt install -y -qq python3-serial python3-bleak python3-requests unclutter curl x11-xserver-utils

# Persistent journal: Pi OS keeps it in RAM by default, so every reboot wiped
# the evidence of a dropout or an HR failure. Capped to spare the SD card.
# Test the drop-in, not the directory: /var/log/journal already exists on Pi
# OS while 40-rpi-volatile-storage.conf still forces Storage=volatile, so the
# old directory test skipped this block and the journal stayed in RAM.
if [ ! -f /etc/systemd/journald.conf.d/treadmill.conf ]; then
  echo "  enabling persistent journal (/var/log/journal, max 200M)"
  sudo mkdir -p /var/log/journal /etc/systemd/journald.conf.d
  printf '[Journal]\nStorage=persistent\nSystemMaxUse=200M\n' \
    | sudo tee /etc/systemd/journald.conf.d/treadmill.conf > /dev/null
  sudo systemd-tmpfiles --create --prefix /var/log/journal
  sudo systemctl restart systemd-journald
fi

# rpi-connect 2.11 ships a PathExists= trigger on auth.key that re-runs the
# oneshot "signin" ~20 times a second forever once signed in: constant fork
# load on a Pi 3B+ and enough log spam to rotate the journal in minutes.
# Masking only the trigger keeps Raspberry Pi Connect itself working.
if systemctl --user cat rpi-connect-signin.path > /dev/null 2>&1 \
   && [ "$(systemctl --user is-enabled rpi-connect-signin.path 2>/dev/null)" != "masked" ]; then
  echo "  masking the looping rpi-connect-signin.path trigger"
  systemctl --user mask --now rpi-connect-signin.path
fi

# Let the dashboard user restart bluetooth.service (and only that) without
# sudo: treadmill_hr.py does it when a strap is heard but refuses the link for
# minutes, and the server unit runs with NoNewPrivileges.
POLKIT_RULE=/etc/polkit-1/rules.d/49-treadmill-bluetooth.rules
if ! sudo test -f "$POLKIT_RULE"; then
  echo "  allowing $(id -un) to restart bluetooth.service (polkit)"
  sudo tee "$POLKIT_RULE" > /dev/null <<EOF
polkit.addRule(function(action, subject) {
  if (action.id == "org.freedesktop.systemd1.manage-units" &&
      action.lookup("unit") == "bluetooth.service" &&
      action.lookup("verb") == "restart" &&
      subject.user == "$(id -un)") {
    return polkit.Result.YES;
  }
});
EOF
fi

# Never let the kernel autosuspend the Feather's USB link (Adafruit VID 239a).
UDEV_RULE=/etc/udev/rules.d/99-treadmill-feather.rules
if [ ! -f "$UDEV_RULE" ]; then
  echo 'ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="239a", TEST=="power/control", ATTR{power/control}="on"' \
    | sudo tee "$UDEV_RULE" > /dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=239a || true
fi

# -------- 3. Vendor JS libs --------
echo "[3/7] Fetching vendor JS libs…"
bash "$WEB/setup.sh"

# -------- 4. Remove the legacy pygame service if present --------
# The v8 dashboard has been retired. If a stale unit file is still on the Pi,
# stop, disable, and remove it so it can't race the web server for the
# Feather's serial port at boot.
echo "[4/7] Removing legacy pygame service (if present)…"
if systemctl list-unit-files --type=service 2>/dev/null | awk '{print $1}' | grep -qx "treadmill-dashboard.service"; then
  sudo systemctl stop    treadmill-dashboard.service 2>/dev/null || true
  sudo systemctl disable treadmill-dashboard.service 2>/dev/null || true
  # Back up the unit before deleting in case the user had local edits
  # (Environment overrides, custom RestartSec, etc.). Manual backups are
  # the only safety net on this Pi — git is not used for /etc/systemd.
  if [ -f /etc/systemd/system/treadmill-dashboard.service ]; then
    sudo cp /etc/systemd/system/treadmill-dashboard.service \
            "/etc/systemd/system/treadmill-dashboard.service.bak-$(date +%Y%m%d-%H%M%S)"
  fi
  sudo rm -f /etc/systemd/system/treadmill-dashboard.service
  sudo systemctl daemon-reload
  echo "  treadmill-dashboard.service removed (backup kept in /etc/systemd/system/)"
else
  echo "  no legacy service present"
fi

# -------- 5. Install systemd unit --------
echo "[5/7] Installing $SERVICE_DST …"
[ -f "$SERVICE_SRC" ] || { echo "  ERROR: $SERVICE_SRC missing."; exit 1; }
mkdir -p "$HOME_DIR/treadmill_logs" "$HOME_DIR/treadmill_exports" "$HOME_DIR/.cache"
if [ ! -f "$HOME_DIR/treadmill_config.json" ]; then
  if [ -f "$ROOT/treadmill_config.json" ]; then
    cp "$ROOT/treadmill_config.json" "$HOME_DIR/treadmill_config.json"
  else
    printf '{}\n' > "$HOME_DIR/treadmill_config.json"
  fi
fi
create_server_unit | sudo tee "$SERVICE_DST" > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable treadmill-server.service
# restart, not just start: an already-running server must pick up the new code
sudo systemctl restart treadmill-server.service

# -------- 6. Smoke test --------
echo "[6/7] Waiting for HTTP server to come up…"
for i in $(seq 1 20); do
  if curl -sf http://localhost:8080/api/health > /dev/null; then
    echo "  /api/health OK"
    break
  fi
  if [ "$i" = "20" ]; then
    echo "  ERROR: server did not respond in 20s. Last status:"
    sudo systemctl status treadmill-server --no-pager | tail -20
    echo "  Logs:"
    sudo journalctl -u treadmill-server --no-pager -n 40
    exit 1
  fi
  sleep 1
done

# -------- 7. Kiosk autostart (user-mode systemd unit) --------
# Previously this installed only the XDG autostart .desktop file, which became
# unreliable on labwc/Wayland — the entry was silently ignored, the kiosk
# never came up after reboot, and the user found themselves looking at an
# empty desktop. We now install a user-mode systemd unit that fires when
# graphical-session.target activates, AND we remove the stale XDG entry.
echo "[7/7] Installing kiosk user systemd unit…"
chmod +x "$WEB/kiosk/treadmill-kiosk.sh"
mkdir -p "$(dirname "$KIOSK_USER_UNIT_DST")"
cp "$KIOSK_USER_UNIT_SRC" "$KIOSK_USER_UNIT_DST"
systemctl --user daemon-reload || true
systemctl --user enable --now treadmill-kiosk.service || true
echo "  $KIOSK_USER_UNIT_DST installed + enabled"

# Linger ensures the user systemd manager keeps the kiosk running across
# logout (it's a kiosk — no real "logout" should happen, but this also
# unblocks `systemctl --user enable` if no graphical session is active yet
# at deploy time).
if ! loginctl show-user "$USER_NAME" 2>/dev/null | grep -q "Linger=yes"; then
  echo "  Enabling lingering for $USER_NAME (requires sudo)…"
  sudo loginctl enable-linger "$USER_NAME" || true
fi

# Remove the legacy XDG autostart entry so it can't race the new unit.
if [ -f "$KIOSK_DESKTOP_DST" ]; then
  rm -f "$KIOSK_DESKTOP_DST"
  echo "  Removed legacy autostart: $KIOSK_DESKTOP_DST"
fi

echo ""
echo "===================================================================="
echo " Deployment complete."
echo ""
echo " Open from your laptop to test now:"
echo "     http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo " Reboot the Pi to start the kiosk on the HyperPixel:"
echo "     sudo reboot"
echo "===================================================================="
