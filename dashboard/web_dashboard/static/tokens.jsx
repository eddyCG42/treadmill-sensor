// Design tokens — adapted from the existing Pygame palette but tightened up.
// All variations stay within these tokens so the look remains coherent.

const TOKENS = {
  // backgrounds
  bg: '#0a0d12',
  bgPanel: '#141a23',
  bgPanelAlt: '#1a212c',
  bgInset: '#0f141b',

  // borders
  border: '#222b39',
  borderStrong: '#2e3a4d',

  // text
  text: '#e8edf5',
  textDim: '#8b95a7',
  textFaint: '#5a6577',

  // semantic
  green: '#22c55e',
  greenDeep: '#16a34a',
  red: '#ef4444',
  redDeep: '#b91c1c',
  amber: '#f59e0b',
  amberDeep: '#b45309',
  blue: '#3b82f6',
  blueDeep: '#1d4ed8',
  purple: '#a855f7',

  // metrics
  pace: '#22c55e',
  incline: '#3b82f6',
  hr: '#ef4444',
  elev: '#f59e0b',
  dist: '#e8edf5',
  cadence: '#a855f7',

  // mode badges
  modePiste: '#f59e0b',   // amber for the predefined track
  modeRandom: '#a855f7',  // purple for randomized
};

// HR zones — copied from the athlete's Nolio zones for "Course à pied - Tapis"
// (read 2026-11-16): Z1 <148, Z2 148-162, Z3 162-175, Z4 175-186, Z5 186-198.
// A zone starts AT its lower bound (148 bpm is Z2), so `max` — each zone's
// UPPER bpm bound, inclusive — is the next zone's start minus one.
// To match on the watch, enter the same bpm bounds as custom zones.
// FCmax 198 (Nolio, set 2026-04-27); FCrepos 56 = 30-day mean of the Garmin
// resting HR synced to Nolio (range 48-61 over 2026-10-18..11-16).
const HR_MAX = 198;
const HR_REST = 56;
const HR_ZONES = [
  { max: 147, color: '#3b82f6', label: 'Z1' },
  { max: 161, color: '#22c55e', label: 'Z2' },
  { max: 174, color: '#f59e0b', label: 'Z3' },
  { max: 185, color: '#ef4444', label: 'Z4' },
  { max: 999, color: '#a855f7', label: 'Z5' },
];

function hrColor(bpm) {
  if (!bpm || bpm <= 0) return TOKENS.textFaint;
  for (const z of HR_ZONES) if (bpm <= z.max) return z.color;
  return TOKENS.purple;
}

function hrZone(bpm) {
  if (!bpm || bpm <= 0) return '--';
  for (const z of HR_ZONES) if (bpm <= z.max) return z.label;
  return 'Z5';
}

function fmtTime(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
  return `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function fmtPace(secPerKm) {
  if (!secPerKm || !isFinite(secPerKm) || secPerKm <= 0) return '--:--';
  const m = Math.floor(secPerKm / 60);
  const s = Math.floor(secPerKm % 60);
  return `${m}:${String(s).padStart(2,'0')}`;
}

function fmtDist(m) {
  if (m < 1000) return { val: Math.round(m).toString(), unit: 'm' };
  return { val: (m / 1000).toFixed(2), unit: 'km' };
}

function paceFromSpeed(kmh) {
  if (!kmh || kmh <= 0.1) return 0;
  return 3600 / kmh;
}

Object.assign(window, { TOKENS, HR_ZONES, HR_MAX, HR_REST, hrColor, hrZone, fmtTime, fmtPace, fmtDist, paceFromSpeed });
