#!/usr/bin/env python3
"""
Real-time cadence detection on the Pi, from the Feather's `$RAW` accel stream.

Why this lives on the Pi (not the Feather): a frame-mounted accelerometer sees
weak, damped, RINGING impacts — a fixed-threshold footfall counter undercounts
walking and double-counts running (measured ~290 spm vs a true ~170). The robust
answer is periodicity, not amplitude: band-pass the step band, then take the
dominant period by autocorrelation over a sliding window, with the belt speed as
a prior to lock the harmonic. Validated on a real recording to ±4 spm from slow
walk (64) to running (171) — see cadence_calibrate.py / the calibration session.

Keeping it here means every knob is a Pi-side config value, tunable at runtime
without ever re-flashing the sealed Feather. The Pi pushes the result with
`BLE_CAD:<spm>`; the firmware broadcasts it to Garmin while fresh, falling back
to its own detector only if the push goes stale.

Pipeline (all cheap — 2 EMAs + one windowed autocorrelation per hop):
    magnitude -> band-pass (EMA_fast - EMA_slow, ~0.7..4.5 Hz)
              -> sliding-window autocorrelation -> dominant lag
              -> speed-prior harmonic pick
              -> presence gate (ACF peak height + plausible stride length)
              -> median + EMA smoothing, decay to 0 when the rhythm is lost.

Stdlib only. Thread model: feed() runs in the serial thread (~100 Hz);
cadence() is read from the tick thread. A small lock guards the published value.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from typing import Callable, Optional


# Physiological cadence bounds (STEPS/min, both feet — the number Garmin shows).
_CAD_MIN_SPM = 55.0
_CAD_MAX_SPM = 205.0


def _prior_cadence(speed_kmh: float) -> tuple[float, float]:
    """(mean, sigma) expected cadence for a belt speed — only used to pick the
    right autocorrelation harmonic and to gate. Two linear branches fitted to
    the calibration recording (walk ≈47+11·kmh, run ≈112+5.4·kmh); the gap
    around 6.8 km/h is the real walk↔run gait transition."""
    if speed_kmh < 1.0:
        return 90.0, 20.0
    if speed_kmh < 6.8:
        return 47.0 + 11.0 * speed_kmh, 14.0
    return 112.0 + 5.4 * speed_kmh, 11.0


# Config keys read live each estimate, with safe fallbacks (all Pi-side; NOT
# Feather SET keys). Defaults mirror treadmill_server.CONFIG_DEFAULTS.
_DEFAULTS = {
    "cad_det_enable": True,
    "cad_win_s": 4.0,        # autocorrelation window (≥2.5 slow cycles)
    "cad_hop_s": 0.25,       # recompute every hop
    "cad_band_lo_hz": 0.7,   # band-pass low cut (kills DC/gravity/sway)
    "cad_band_hi_hz": 4.5,   # band-pass high cut (kills HF frame ringing)
    "cad_conf_min": 0.18,    # ACF peak-height gate (presence)
    "cad_sl_min_m": 0.35,    # implausibly short stride -> reject (empty belt)
    "cad_sl_max_m": 2.8,     # implausibly long stride  -> reject
    "cad_hold_ms": 2500,     # hold last cadence this long after losing rhythm
    "cad_smooth": 0.30,      # output EMA weight on each new valid estimate
}


class CadenceDetector:
    def __init__(self, cfg_provider: Optional[Callable[[], dict]] = None) -> None:
        self._get_cfg = cfg_provider
        self._lock = threading.Lock()

        # band-pass state
        self._es = None          # slow EMA
        self._ef = None          # fast EMA
        self._a_slow = 0.044
        self._a_fast = 0.283
        self._band_key = None    # (lo, hi, dt) the alphas were computed for

        # timing
        self._last_ms = None
        self._dt = 0.01          # EMA of sample interval (s)

        # ring buffers (band signal + belt speed m/s), sized to the window
        self._win_n = 400
        self._band = deque(maxlen=self._win_n)
        self._spd = deque(maxlen=self._win_n)
        self._since_hop = 0
        self._hop_n = 25

        # published output
        self._out_spm = 0.0
        self._conf = 0.0
        self._last_valid_t = 0.0   # monotonic-ish (fed by server clock via feed)
        self._recent = deque(maxlen=3)
        self._now = 0.0            # last sample's wall time (set by feed)

    def bind(self, cfg_provider: Callable[[], dict]) -> None:
        self._get_cfg = cfg_provider

    def _cfg(self, key: str):
        if self._get_cfg is not None:
            try:
                v = self._get_cfg().get(key)
                if v is not None:
                    return v
            except Exception:
                pass
        return _DEFAULTS[key]

    def reset(self) -> None:
        """Clear buffers on (re)connect so a stale window can't leak cadence."""
        with self._lock:
            self._es = self._ef = None
            self._last_ms = None
            self._band.clear()
            self._spd.clear()
            self._recent.clear()
            self._since_hop = 0
            self._out_spm = 0.0
            self._conf = 0.0

    # ---- ingestion (serial thread) --------------------------------------
    def feed(self, feather_ms: int, ax: float, ay: float, az: float,
             speed_ms: float, now: float) -> None:
        """One $RAW sample. ax/ay/az in m/s², speed in m/s, now = wall clock."""
        if not self._cfg("cad_det_enable"):
            return
        mag = math.sqrt(ax * ax + ay * ay + az * az)

        # adaptive sample interval from the Feather's own millis
        if self._last_ms is not None:
            d = (feather_ms - self._last_ms) / 1000.0
            if 0.0 < d < 0.5:
                self._dt += 0.05 * (d - self._dt)
        self._last_ms = feather_ms
        self._now = now

        self._refresh_bandpass()
        if self._es is None:
            self._es = self._ef = mag
        self._es += self._a_slow * (mag - self._es)
        self._ef += self._a_fast * (mag - self._ef)

        self._resize_window()
        self._band.append(self._ef - self._es)
        self._spd.append(speed_ms)

        self._since_hop += 1
        if self._since_hop >= self._hop_n and len(self._band) >= self._win_n:
            self._since_hop = 0
            self._estimate()

    def _refresh_bandpass(self) -> None:
        lo = float(self._cfg("cad_band_lo_hz"))
        hi = float(self._cfg("cad_band_hi_hz"))
        key = (lo, hi, round(self._dt, 5))
        if key != self._band_key:
            self._a_slow = 1.0 - math.exp(-2.0 * math.pi * lo * self._dt)
            self._a_fast = 1.0 - math.exp(-2.0 * math.pi * hi * self._dt)
            self._band_key = key

    def _resize_window(self) -> None:
        win_n = max(64, int(float(self._cfg("cad_win_s")) / self._dt))
        hop_n = max(4, int(float(self._cfg("cad_hop_s")) / self._dt))
        self._hop_n = hop_n
        if win_n != self._win_n:
            self._win_n = win_n
            self._band = deque(self._band, maxlen=win_n)
            self._spd = deque(self._spd, maxlen=win_n)

    # ---- estimation -----------------------------------------------------
    def _estimate(self) -> None:
        band = list(self._band)
        spd = list(self._spd)
        dt = self._dt
        n = len(band)

        spd_sorted = sorted(spd)
        v_ms = spd_sorted[len(spd_sorted) // 2]
        kmh = v_ms * 3.6
        mu, sig = _prior_cadence(kmh)

        cad, conf = self._acf(band, dt, mu)

        sl = (2.0 * v_ms) / (cad / 60.0) if cad > 0 else 0.0  # implied stride (m)
        conf_min = float(self._cfg("cad_conf_min"))
        sl_min = float(self._cfg("cad_sl_min_m"))
        sl_max = float(self._cfg("cad_sl_max_m"))
        valid = (conf >= conf_min and sl_min <= sl <= sl_max
                 and abs(cad - mu) <= 3.0 * sig)

        with self._lock:
            self._conf = conf
            if valid:
                self._recent.append(cad)
                med = sorted(self._recent)[len(self._recent) // 2]
                a = float(self._cfg("cad_smooth"))
                if self._out_spm < 1.0:
                    self._out_spm = med
                else:
                    self._out_spm += a * (med - self._out_spm)
                self._last_valid_t = self._now
            else:
                self._recent.clear()
                hold = float(self._cfg("cad_hold_ms")) / 1000.0
                if self._now - self._last_valid_t > hold:
                    self._out_spm = 0.0

    def _acf(self, seg, dt, prior_spm):
        """Dominant cadence via normalized autocorrelation, speed-prior pick.
        Returns (spm, confidence 0..1). Confidence = ACF peak height."""
        n = len(seg)
        lag_min = max(2, int(round((60.0 / _CAD_MAX_SPM) / dt)))
        lag_max = min(n // 2, int(round((60.0 / _CAD_MIN_SPM) / dt)))
        if lag_max <= lag_min + 1:
            return 0.0, 0.0
        mean = sum(seg) / n
        x = [v - mean for v in seg]
        denom = sum(v * v for v in x) or 1.0

        peaks = []
        p2 = p1 = None
        for lag in range(lag_min, lag_max + 1):
            s = 0.0
            # inner correlation; local, tight loop
            for k in range(n - lag):
                s += x[k] * x[k + lag]
            val = s / denom
            if p1 is not None and p2 is not None and p1 > p2 and p1 >= val and p1 > 0:
                peaks.append((lag - 1, p1))
            p2, p1 = p1, val
        if not peaks:
            return 0.0, 0.0

        def spm_of(lag):
            return 60.0 / (lag * dt)

        def score(p):
            c = spm_of(p[0])
            if c < _CAD_MIN_SPM or c > _CAD_MAX_SPM:
                return -1.0
            dist = abs(math.log(c / prior_spm)) if prior_spm > 0 else 0.0
            return p[1] * math.exp(-2.0 * dist)

        best = max(peaks, key=score)
        return spm_of(best[0]), max(0.0, min(1.0, best[1]))

    # ---- readout (tick thread) ------------------------------------------
    def cadence(self, now: Optional[float] = None):
        """(spm:int, confidence:float, fresh:bool). fresh means the detector
        currently holds a confident reading: it produced a valid estimate within
        the hold window AND the raw stream is alive. When not fresh (disabled,
        sub-threshold walking, stream dropped) the caller falls back to the
        Feather's own broadcast cadence instead of pushing/using this one."""
        if not self._cfg("cad_det_enable"):
            return 0, 0.0, False
        hold = float(self._cfg("cad_hold_ms")) / 1000.0
        with self._lock:
            spm = int(round(self._out_spm))
            conf = self._conf
            age = (now - self._last_valid_t) if now is not None else 0.0
            fresh = spm >= 1 and self._last_valid_t > 0.0 and age <= hold
        return spm, conf, fresh


# module-level singleton, mirrors raw_recorder in treadmill_server
cadence_detector = CadenceDetector()
