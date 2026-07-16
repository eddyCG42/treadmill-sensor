#!/bin/bash
# ----------------------------------------------------------------------
# Treadmill web dashboard — one-time setup
# Downloads vendored JS libraries (React, ReactDOM, Babel) AND the two
# webfonts (Barlow, JetBrains Mono) into static/vendor/.
# Verifies SHA-384 hashes when configured so a CDN compromise can't sneak
# malicious JS into the kiosk (which holds Strava OAuth tokens).
# Run once after cloning. Idempotent — re-running re-verifies and re-fetches
# only what's missing.
#
# How to pin a new file:
#   1. Run setup.sh — it'll print the SHA-384 of every unpinned file
#   2. Paste the hash into the corresponding line of the FILES / FONTS
#      array below (the third pipe-delimited field)
#   3. Re-run setup.sh to confirm verification passes
# ----------------------------------------------------------------------

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENDOR="$HERE/static/vendor"
FONTS_DIR="$VENDOR/fonts"
mkdir -p "$VENDOR" "$FONTS_DIR"

# JS files: name | url | sha384 (empty = unpinned, will print hash and warn)
FILES=(
  "react.production.min.js|https://unpkg.com/react@18.3.1/umd/react.production.min.js|"
  "react-dom.production.min.js|https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js|"
  "babel.min.js|https://unpkg.com/@babel/standalone@7.29.0/babel.min.js|"
)

# Webfont files: name | url | sha384 (empty = unpinned)
# Fontsource on jsDelivr ships fixed-name woff2 files per weight, pinned by package version.
FONT_VERSION_BARLOW="5.2.5"
FONT_VERSION_JETBRAINS="5.2.5"
FONTS=(
  "barlow-400.woff2|https://cdn.jsdelivr.net/npm/@fontsource/barlow@${FONT_VERSION_BARLOW}/files/barlow-latin-400-normal.woff2|"
  "barlow-500.woff2|https://cdn.jsdelivr.net/npm/@fontsource/barlow@${FONT_VERSION_BARLOW}/files/barlow-latin-500-normal.woff2|"
  "barlow-600.woff2|https://cdn.jsdelivr.net/npm/@fontsource/barlow@${FONT_VERSION_BARLOW}/files/barlow-latin-600-normal.woff2|"
  "barlow-700.woff2|https://cdn.jsdelivr.net/npm/@fontsource/barlow@${FONT_VERSION_BARLOW}/files/barlow-latin-700-normal.woff2|"
  "barlow-800.woff2|https://cdn.jsdelivr.net/npm/@fontsource/barlow@${FONT_VERSION_BARLOW}/files/barlow-latin-800-normal.woff2|"
  "jetbrains-mono-400.woff2|https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@${FONT_VERSION_JETBRAINS}/files/jetbrains-mono-latin-400-normal.woff2|"
  "jetbrains-mono-600.woff2|https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@${FONT_VERSION_JETBRAINS}/files/jetbrains-mono-latin-600-normal.woff2|"
  "jetbrains-mono-700.woff2|https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@${FONT_VERSION_JETBRAINS}/files/jetbrains-mono-latin-700-normal.woff2|"
)

echo "[setup] Vendor directory: $VENDOR"

# Pick a downloader
if command -v curl > /dev/null; then
  DL() { curl -fsSL --retry 3 --retry-delay 2 -o "$2" "$1"; }
elif command -v wget > /dev/null; then
  DL() { wget -q --tries=3 -O "$2" "$1"; }
else
  echo "[setup] ERROR: neither curl nor wget is available."
  echo "        Install one with: sudo apt install -y curl"
  exit 1
fi

# SHA-384 helper. Uses openssl if available, falls back to sha384sum.
if command -v openssl > /dev/null; then
  SHA384() { openssl dgst -sha384 -binary "$1" | openssl base64 -A; }
else
  SHA384() { sha384sum "$1" | cut -d' ' -f1; }
fi

# Process a single file entry: download, sanity-check, hash, verify-or-warn.
# Args: dest_dir, "name|url|expected_sha"
fetch_one() {
  local dest_dir="$1"
  local entry="$2"
  local name url expected
  name="${entry%%|*}"
  rest="${entry#*|}"
  url="${rest%|*}"
  expected="${rest##*|}"
  local out="$dest_dir/$name"

  if [ ! -s "$out" ]; then
    echo "[setup] FETCH $name  <-  $url"
    DL "$url" "$out"
  fi

  # Sanity: not an HTML error page, not tiny
  if head -c 200 "$out" | grep -qi "<html\|<!doctype"; then
    echo "[setup] ERROR: $name looks like HTML — fetch failed."
    rm -f "$out"
    exit 1
  fi
  size=$(stat -c%s "$out" 2>/dev/null || stat -f%z "$out")
  if [ "$size" -lt 200 ]; then
    echo "[setup] ERROR: $name is too small ($size bytes) — fetch failed."
    rm -f "$out"
    exit 1
  fi

  # Hash verification
  local actual
  actual="$(SHA384 "$out")"
  if [ -z "$expected" ]; then
    echo "[setup] WARN  $name  unpinned — SHA-384=$actual  (paste into setup.sh to pin)"
  elif [ "$expected" = "$actual" ]; then
    echo "[setup] OK    $name  ($(du -h "$out" | cut -f1))  pinned"
  else
    echo "[setup] ERROR $name  SHA-384 mismatch!"
    echo "        expected: $expected"
    echo "        got:      $actual"
    echo "        File on disk MAY be tampered. Deleting; re-run setup.sh after investigation."
    rm -f "$out"
    exit 1
  fi
}

echo "[setup] === JS libraries ==="
for entry in "${FILES[@]}"; do
  fetch_one "$VENDOR" "$entry"
done

echo "[setup] === Webfonts ==="
for entry in "${FONTS[@]}"; do
  fetch_one "$FONTS_DIR" "$entry"
done

# Write the local fonts.css that dashboard.html @imports.
# Keeping this generated (not committed) keeps the @font-face URLs in lockstep
# with whatever files we actually downloaded.
cat > "$FONTS_DIR/fonts.css" <<'CSS'
/* Auto-generated by setup.sh — do not edit by hand. */
@font-face { font-family: 'Barlow'; font-style: normal; font-weight: 400; font-display: swap; src: url('./barlow-400.woff2') format('woff2'); }
@font-face { font-family: 'Barlow'; font-style: normal; font-weight: 500; font-display: swap; src: url('./barlow-500.woff2') format('woff2'); }
@font-face { font-family: 'Barlow'; font-style: normal; font-weight: 600; font-display: swap; src: url('./barlow-600.woff2') format('woff2'); }
@font-face { font-family: 'Barlow'; font-style: normal; font-weight: 700; font-display: swap; src: url('./barlow-700.woff2') format('woff2'); }
@font-face { font-family: 'Barlow'; font-style: normal; font-weight: 800; font-display: swap; src: url('./barlow-800.woff2') format('woff2'); }
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 400; font-display: swap; src: url('./jetbrains-mono-400.woff2') format('woff2'); }
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 600; font-display: swap; src: url('./jetbrains-mono-600.woff2') format('woff2'); }
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 700; font-display: swap; src: url('./jetbrains-mono-700.woff2') format('woff2'); }
CSS

echo "[setup] Wrote $FONTS_DIR/fonts.css"
echo "[setup] Done."
