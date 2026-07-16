#!/usr/bin/env python3
"""
Offline cadence calibration from a `cadraw_*.csv` recording.

Input CSV (written by the dashboard's CADENCE tab recorder):
    pi_time,feather_ms,ax_mms2,ay_mms2,az_mms2,speed_mms

What it does
------------
1. Builds the impact envelope from the frame accelerometer (|accel| minus a
   slow baseline — same idea as the firmware's footfall detector).
2. Finds stable belt-speed plateaus (the protocol's held segments).
3. Per segment, estimates cadence by AUTOCORRELATION of the envelope, using the
   belt speed as a prior to reject the ½x / 2x harmonic traps that fool FFT and
   fixed-threshold counters.
4. Recommends the three firmware detection knobs (cad_threshold / cad_rearm /
   cad_refract_ms) from the measured noise floor vs. step-peak statistics, and
   prints a cadence-vs-speed table to build the model.

Stdlib only (no numpy/scipy) so it runs anywhere, including the Pi.

Usage
-----
    python3 cadence_calibrate.py cadraw_20260714_190000.csv
    # optional ground-truth counts to lock the harmonic (speed_kmh:spm , ...):
    python3 cadence_calibrate.py rec.csv --truth 5.5:112,9.0:164
    # if the belt speed in the CSV is raw (pre pace_factor), pass the factor:
    python3 cadence_calibrate.py rec.csv --pace-factor 1.01
"""

import argparse
import csv
import math
import statistics


# Plausible per-FOOT... no — cadence here is STEPS/min (both feet), the number
# Garmin shows. Walking ~80-120, running ~150-190, sprint up to ~220.
CAD_MIN_SPM = 55.0
CAD_MAX_SPM = 220.0


def read_csv(path):
    t, mag, spd = [], [], []
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                ms = float(row["feather_ms"])
                ax = float(row["ax_mms2"]) / 1000.0
                ay = float(row["ay_mms2"]) / 1000.0
                az = float(row["az_mms2"]) / 1000.0
                sp = float(row["speed_mms"]) / 1000.0
            except (KeyError, ValueError):
                continue
            t.append(ms / 1000.0)
            mag.append(math.sqrt(ax * ax + ay * ay + az * az))
            spd.append(sp)
    return t, mag, spd


def sample_dt(t):
    d = [t[i] - t[i - 1] for i in range(1, len(t)) if 0 < t[i] - t[i - 1] < 1.0]
    return statistics.median(d) if d else 0.01


def envelope(mag, alpha=0.02):
    """|accel| minus slow EMA baseline, rectified — the footfall signal."""
    base = mag[0] if mag else 0.0
    env = []
    for m in mag:
        base = (1 - alpha) * base + alpha * m
        env.append(abs(m - base))
    return env


def find_segments(t, spd, min_dur=12.0, tol_kmh=0.6):
    """Stable belt-speed plateaus: runs where speed stays within tol for min_dur."""
    segs = []
    n = len(spd)
    i = 0
    while i < n:
        j = i + 1
        lo = hi = spd[i]
        while j < n:
            lo = min(lo, spd[j])
            hi = max(hi, spd[j])
            if (hi - lo) * 3.6 > tol_kmh:
                break
            j += 1
        if t[j - 1] - t[i] >= min_dur:
            segs.append((i, j))
        i = j
    return segs


def acf_cadence(env, dt, speed_kmh, truth=None):
    """Dominant step period via autocorrelation, disambiguated by belt speed.
    Returns (cadence_spm, confidence 0..1)."""
    lag_min = int(round((60.0 / CAD_MAX_SPM) / dt))
    lag_max = int(round((60.0 / CAD_MIN_SPM) / dt))
    lag_max = min(lag_max, len(env) // 2)
    if lag_max <= lag_min + 1:
        return 0.0, 0.0

    mean = sum(env) / len(env)
    x = [v - mean for v in env]
    denom = sum(v * v for v in x) or 1.0

    # autocorrelation over the candidate lag window
    acf = []
    for lag in range(lag_min, lag_max + 1):
        s = 0.0
        for k in range(len(x) - lag):
            s += x[k] * x[k + lag]
        acf.append((lag, s / denom))

    # candidate peaks (local maxima)
    peaks = []
    for idx in range(1, len(acf) - 1):
        lag, val = acf[idx]
        if val > acf[idx - 1][1] and val >= acf[idx + 1][1] and val > 0:
            peaks.append((lag, val))
    if not peaks:
        return 0.0, 0.0

    def spm_of(lag):
        return 60.0 / (lag * dt)

    # belt-speed prior: expected cadence grows with speed. Rough model good
    # enough to pick the right harmonic (walk ~1.3 SL, run ~ speed-dependent).
    exp = expected_cadence(speed_kmh)
    if truth is not None:
        exp = truth

    def score(p):
        lag, val = p
        c = spm_of(lag)
        if c < CAD_MIN_SPM or c > CAD_MAX_SPM:
            return -1
        # prefer strong ACF near the expected cadence (log-ratio distance)
        dist = abs(math.log(c / exp)) if exp > 0 else 0.0
        return val * math.exp(-2.0 * dist)

    best = max(peaks, key=score)
    conf = max(0.0, min(1.0, best[1]))  # ACF peak height is already normalised
    return spm_of(best[0]), conf


def expected_cadence(speed_kmh):
    """Very rough cadence prior from belt speed, only to pick the harmonic.
    Walk (<7 km/h): ~100-120. Run: ~150 + slope. Clamped to sane range."""
    if speed_kmh < 1.0:
        return 90.0
    if speed_kmh < 7.0:
        c = 90.0 + speed_kmh * 5.0        # 90..125
    else:
        c = 150.0 + (speed_kmh - 8.0) * 3.0  # ~150 at 8 km/h, +3/kmh
    return max(70.0, min(200.0, c))


def peak_stats(env, dt, refract_s=0.20):
    """Median height of impact peaks + noise floor, for threshold picking."""
    peaks = []
    last = -1e9
    for i in range(1, len(env) - 1):
        if env[i] > env[i - 1] and env[i] >= env[i + 1]:
            if (i * dt) - last >= refract_s:
                peaks.append(env[i])
                last = i * dt
    noise = statistics.median(sorted(env)[: max(1, len(env) // 5)])  # low quintile
    pk = statistics.median(peaks) if peaks else 0.0
    return noise, pk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--truth", default="", help="speed:spm pairs, e.g. 5.5:112,9:164")
    ap.add_argument("--pace-factor", type=float, default=1.0)
    ap.add_argument("--min-seg", type=float, default=12.0, help="min plateau seconds")
    args = ap.parse_args()

    truth = {}
    for pair in args.truth.split(","):
        if ":" in pair:
            s, c = pair.split(":")
            truth[float(s)] = float(c)

    t, mag, spd = read_csv(args.csv)
    if len(t) < 200:
        print(f"Too few samples ({len(t)}). Is the CSV the raw recording?")
        return
    dt = sample_dt(t)
    fs = 1.0 / dt
    dur = t[-1] - t[0]
    print(f"Loaded {len(t)} samples, {dur:.0f}s, ~{fs:.0f} Hz\n")

    env = envelope(mag)
    segs = find_segments(t, spd, min_dur=args.min_seg)
    if not segs:
        print("No stable speed plateaus found — hold each speed steadier/longer.")
        return

    print(f"{'speed':>7} {'dur':>5} {'cadence':>8} {'conf':>5}  {'note'}")
    print("-" * 48)
    rows = []
    all_noise, all_pk, all_spm = [], [], []
    for (a, b) in segs:
        seg_env = env[a:b]
        seg_spd_ms = statistics.median(spd[a:b])
        speed_kmh = seg_spd_ms * 3.6 * args.pace_factor
        seg_dur = t[b - 1] - t[a]

        # nearest ground-truth anchor (within 0.8 km/h)
        gt = None
        for s, c in truth.items():
            if abs(s - speed_kmh) <= 0.8:
                gt = c
        cad, conf = acf_cadence(seg_env, dt, speed_kmh, truth=gt)
        noise, pk = peak_stats(seg_env, dt)

        note = ""
        if speed_kmh < 0.5:
            note = "STILL (noise floor)"
            all_noise.append(noise)
        else:
            all_pk.append(pk)
            if cad > 0:
                all_spm.append(cad)
        if gt is not None:
            note += f" [truth {gt:.0f}, err {cad-gt:+.0f}]"

        print(f"{speed_kmh:6.1f}k {seg_dur:4.0f}s {cad:7.0f} {conf:5.2f}  {note}")
        rows.append((speed_kmh, cad, conf, noise, pk, seg_dur))

    # ---- recommended firmware params ----
    print("\n=== recommended SET values (push over serial, no reflash) ===")
    noise_floor = statistics.median(all_noise) if all_noise else (
        min(r[3] for r in rows) if rows else 0.5)
    pk_med = statistics.median(all_pk) if all_pk else 3.0
    thr = round(noise_floor + 0.35 * max(0.1, pk_med - noise_floor), 2)
    thr = max(0.5, thr)
    rearm = round(thr / 2.0, 2)
    max_spm = max(all_spm) if all_spm else 190.0
    # refractory: safely under the shortest real step interval
    refract = int(1000.0 * (60.0 / max_spm) * 0.55)
    refract = max(120, min(300, refract))
    print(f"  noise floor ~{noise_floor:.2f} m/s^2, step peak ~{pk_med:.2f} m/s^2")
    print(f"  SET cad_threshold {thr}")
    print(f"  SET cad_rearm {rearm}")
    print(f"  SET cad_refract_ms {refract}")

    # ---- cadence vs speed (for a model / sanity) ----
    print("\n=== cadence vs speed ===")
    for speed_kmh, cad, conf, *_ in sorted(rows):
        if speed_kmh >= 0.5 and cad > 0:
            bar = "#" * int(cad / 5)
            print(f"  {speed_kmh:5.1f} km/h -> {cad:5.0f} spm  {bar}")


if __name__ == "__main__":
    main()
