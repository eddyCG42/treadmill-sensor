#!/bin/bash
# ----------------------------------------------------------------------
# Treadmill kiosk launcher — Chromium fullscreen on HyperPixel 4.0.
# Invoked by:
#   ~/.config/systemd/user/treadmill-kiosk.service   (current, recommended)
#   ~/.config/autostart/treadmill-kiosk.desktop      (legacy, unreliable on labwc)
# ----------------------------------------------------------------------

set -u

URL="http://localhost:8080/"

# Wait for the local server (max 60s — was 30, bumped because the BLE
# initialization delay can push first-response back a bit).
for i in $(seq 1 60); do
  if curl -sf "${URL}api/health" > /dev/null 2>&1; then
    echo "[treadmill-kiosk] server ready after ${i}s"
    break
  fi
  sleep 1
done

# Best-effort screen-blanking suppression — X11 only.
# On Wayland sessions (Pi OS Bookworm/Trixie default for Pi 4+), these
# commands either don't exist or no-op. We don't fail if they aren't there.
if [ -n "${DISPLAY:-}" ] && command -v xset >/dev/null 2>&1; then
  xset s off       2>/dev/null || true
  xset s noblank   2>/dev/null || true
  xset -dpms       2>/dev/null || true
fi
if [ -n "${DISPLAY:-}" ] && command -v unclutter >/dev/null 2>&1; then
  unclutter -idle 0.5 -root 2>/dev/null &
fi

# Kill any previous chromium so a re-run is clean
pkill -f "chromium-browser.*--kiosk" 2>/dev/null || true
pkill -f "chromium.*--kiosk"         2>/dev/null || true
sleep 0.5

# Pick chromium binary (different package names across Pi OS versions)
CHROME=""
for c in chromium chromium-browser /usr/bin/chromium /usr/bin/chromium-browser; do
  if command -v "$c" > /dev/null 2>&1; then
    CHROME="$c"
    break
  fi
done
if [ -z "$CHROME" ]; then
  echo "[treadmill-kiosk] chromium not found — install with: sudo apt install chromium" >&2
  exit 1
fi

# Clean profile each launch avoids the 'restore session' prompt after a hard reboot
PROFILE="/tmp/treadmill-kiosk-profile"
rm -rf "$PROFILE"
mkdir -p "$PROFILE"

echo "[treadmill-kiosk] launching $CHROME -> $URL"
echo "[treadmill-kiosk] DISPLAY=${DISPLAY:-<unset>}  WAYLAND_DISPLAY=${WAYLAND_DISPLAY:-<unset>}"

exec "$CHROME" \
  --kiosk \
  --noerrdialogs \
  --disable-translate \
  --disable-infobars \
  --disable-features=TranslateUI,OverscrollHistoryNavigation \
  --no-first-run \
  --fast \
  --fast-start \
  --disable-pinch \
  --overscroll-history-navigation=0 \
  --check-for-update-interval=31536000 \
  --password-store=basic \
  --disable-sync \
  --disable-features=PasswordManagerEnabled \
  --user-data-dir="$PROFILE" \
  --window-size=800,480 \
  --window-position=0,0 \
  --app="$URL"
