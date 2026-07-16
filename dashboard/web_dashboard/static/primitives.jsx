// Shared primitives — buttons, metric tiles, status dots.
// Designed for an 800x480 touch screen, min hit target 48px.

const { useState, useEffect, useRef } = React;

function Btn({ children, onClick, color = 'neutral', size = 'md', wide = false, disabled = false, style = {}, active = false }) {
  const palette = {
    neutral: { bg: '#1f2937', bgHover: '#2a3544', fg: '#e8edf5', border: '#2e3a4d' },
    green:   { bg: '#16a34a', bgHover: '#22c55e', fg: '#fff', border: '#15803d' },
    red:     { bg: '#b91c1c', bgHover: '#ef4444', fg: '#fff', border: '#991b1b' },
    amber:   { bg: '#b45309', bgHover: '#d97706', fg: '#fff', border: '#92400e' },
    blue:    { bg: '#1d4ed8', bgHover: '#3b82f6', fg: '#fff', border: '#1e40af' },
    purple:  { bg: '#7e22ce', bgHover: '#a855f7', fg: '#fff', border: '#6b21a8' },
    ghost:   { bg: 'transparent', bgHover: '#1a212c', fg: '#e8edf5', border: '#2e3a4d' },
  }[color] || { bg: '#1f2937', bgHover: '#2a3544', fg: '#e8edf5', border: '#2e3a4d' };

  const heights = { sm: 36, md: 48, lg: 56, xl: 68 };
  const pads = { sm: '0 12px', md: '0 16px', lg: '0 20px', xl: '0 24px' };
  const fonts = { sm: 14, md: 17, lg: 20, xl: 24 };

  const [hover, setHover] = useState(false);
  const [press, setPress] = useState(false);

  return (
    <button
      onClick={disabled ? undefined : onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => { setHover(false); setPress(false); }}
      onMouseDown={() => setPress(true)}
      onMouseUp={() => setPress(false)}
      disabled={disabled}
      style={{
        height: heights[size],
        padding: pads[size],
        width: wide ? '100%' : 'auto',
        background: active ? palette.bgHover : (hover ? palette.bgHover : palette.bg),
        color: palette.fg,
        border: `1.5px solid ${active ? palette.fg : palette.border}`,
        borderRadius: 10,
        fontSize: fonts[size],
        fontWeight: 700,
        letterSpacing: 0.3,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.45 : 1,
        transform: press ? 'translateY(1px)' : 'none',
        transition: 'background 0.08s, transform 0.05s',
        fontFamily: 'inherit',
        display: 'inline-flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 6,
        ...style,
      }}
    >
      {children}
    </button>
  );
}

function Panel({ children, style = {}, padded = true }) {
  return (
    <div style={{
      background: TOKENS.bgPanel,
      border: `1px solid ${TOKENS.border}`,
      borderRadius: 14,
      padding: padded ? 14 : 0,
      ...style,
    }}>
      {children}
    </div>
  );
}

function Label({ children, color = TOKENS.textDim, style = {} }) {
  return (
    <div style={{
      fontSize: 11,
      fontWeight: 700,
      letterSpacing: 1.4,
      textTransform: 'uppercase',
      color,
      ...style,
    }}>
      {children}
    </div>
  );
}

// Big-number metric with label and unit. Used heavily on the dashboard.
function Metric({ label, value, unit, color = TOKENS.text, valueSize = 44, sub = null, align = 'left', style = {} }) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: align === 'center' ? 'center' : 'flex-start',
      justifyContent: 'center',
      ...style,
    }}>
      <Label>{label}</Label>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginTop: 2 }}>
        <div style={{
          fontSize: valueSize,
          fontWeight: 800,
          lineHeight: 1,
          color,
          fontVariantNumeric: 'tabular-nums',
          letterSpacing: -1,
        }}>
          {value}
        </div>
        {unit && (
          <div style={{ fontSize: Math.max(13, valueSize * 0.32), color: TOKENS.textDim, fontWeight: 600 }}>
            {unit}
          </div>
        )}
      </div>
      {sub && <div style={{ fontSize: 12, color: TOKENS.textFaint, marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function Dot({ color, pulse = false }) {
  return (
    <span style={{
      display: 'inline-block',
      width: 8,
      height: 8,
      borderRadius: '50%',
      background: color,
      boxShadow: pulse ? `0 0 0 0 ${color}` : 'none',
      animation: pulse ? 'pulse 1.4s infinite' : 'none',
      flexShrink: 0,
    }} />
  );
}

// Stepper button cluster — used for pace ±0.01/0.1/1 and incline ±0.5/1.
function Stepper({ steps, onStep, color = 'green', size = 'md' }) {
  // steps: [{ label, delta }]
  return (
    <div style={{ display: 'flex', gap: 6 }}>
      {steps.map((s, i) => (
        <Btn key={i} color={color} size={size} onClick={() => onStep(s.delta)} style={{ minWidth: size === 'sm' ? 48 : 58, padding: '0 8px' }}>
          {s.label}
        </Btn>
      ))}
    </div>
  );
}

// Mode badge — shows the active session mode at all times.
function ModeBadge({ mode, compact = false, onClick = null }) {
  const cfg = mode === 'piste'
    ? { color: TOKENS.modePiste, label: 'PISTE', sub: 'Parcours fixe' }
    : { color: TOKENS.modeRandom, label: 'RANDOM', sub: 'Parcours aléatoire' };
  return (
    <div
      onClick={onClick}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 8,
        padding: compact ? '4px 10px' : '6px 12px',
        background: `${cfg.color}1a`,
        border: `1.5px solid ${cfg.color}`,
        borderRadius: 999,
        cursor: onClick ? 'pointer' : 'default',
      }}
    >
      <span style={{
        width: 8, height: 8, borderRadius: '50%', background: cfg.color,
      }} />
      <span style={{ fontSize: compact ? 12 : 13, fontWeight: 800, letterSpacing: 1, color: cfg.color }}>
        {cfg.label}
      </span>
      {!compact && <span style={{ fontSize: 11, color: TOKENS.textDim, marginLeft: 2 }}>{cfg.sub}</span>}
    </div>
  );
}

// HR ring — visualizes current zone.
function HRRing({ bpm, size = 60 }) {
  const c = hrColor(bpm);
  // Fill spans resting→max HR (64→198) so the ring tracks % HR reserve.
  const pct = Math.min(1, Math.max(0, (bpm - 64) / (198 - 64)));
  const r = (size - 8) / 2;
  const circ = 2 * Math.PI * r;
  return (
    <svg width={size} height={size}>
      <circle cx={size/2} cy={size/2} r={r} stroke="#1a212c" strokeWidth={4} fill="none" />
      <circle
        cx={size/2} cy={size/2} r={r}
        stroke={c} strokeWidth={4} fill="none"
        strokeDasharray={`${circ * pct} ${circ}`}
        strokeLinecap="round"
        transform={`rotate(-90 ${size/2} ${size/2})`}
      />
    </svg>
  );
}

// Status bar dot with label.
function StatusItem({ label, color, value, pulse = false }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 5 }}>
      <Dot color={color} pulse={pulse} />
      <span style={{ fontSize: 11, color: TOKENS.textDim, fontWeight: 600 }}>{label}</span>
      {value && <span style={{ fontSize: 11, color: TOKENS.text, fontWeight: 700 }}>{value}</span>}
    </div>
  );
}

Object.assign(window, { Btn, Panel, Label, Metric, Dot, Stepper, ModeBadge, HRRing, StatusItem });
