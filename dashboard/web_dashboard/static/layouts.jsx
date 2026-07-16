// Main dashboard — IDLE / RUNNING / PAUSED states.
// Layout variants exposed via tweaks: 'cockpit', 'focus', 'split'

const { useState: useStateD, useEffect: useEffectD } = React;

// Persistent bottom bar. During a run this is the only status strip visible
// (the idle title bar is hidden), so it must show REAL connection + storage
// state — it used to render hardcoded mock values (USB always green, "13 runs").
function StatusBar({ state, data = {} }) {
  const stateColors = {
    IDLE: TOKENS.textDim,
    RUNNING: TOKENS.green,
    PAUSED: TOKENS.amber
  };
  const stateLabel = {
    IDLE: 'IDLE',
    RUNNING: 'REC',
    PAUSED: 'AUTO-PAUSE'
  };
  const bleOn = data.ble_on;
  const usbColor = data.usb_connected ? TOKENS.green : TOKENS.textFaint;
  const bleColor = bleOn ? (data.ble_connected ? TOKENS.green : TOKENS.amber) : TOKENS.textFaint;
  const hrColor_ = data.hr_connected ? TOKENS.green : TOKENS.textFaint;
  const runs = data.log_count || 0;
  const usedMb = Math.round(data.logs_size_mb || 0);
  const freeMb = data.disk_free_mb || 0;
  const freeStr = freeMb >= 1000 ? (freeMb / 1000).toFixed(1) + 'G' : Math.round(freeMb) + 'M';
  return (
    <div style={{
      height: 28,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '0 12px',
      background: TOKENS.bgInset,
      borderTop: `1px solid ${TOKENS.border}`,
      fontSize: 11,
      fontFamily: 'inherit',
      flexShrink: 0,
      whiteSpace: 'nowrap',
      overflow: 'hidden'
    }}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <StatusItem label="USB" color={usbColor} />
        <StatusItem label="BLE" color={bleColor} pulse={bleOn && !data.ble_connected} />
        <StatusItem label="HR" color={hrColor_} />
        {data.usb_connected && data.tof_ok === false &&
          <StatusItem label="TOF" color={TOKENS.red} pulse />}
        <span style={{
          fontSize: 11,
          fontWeight: 800,
          letterSpacing: 1,
          color: stateColors[state],
          marginLeft: 4
        }}>
          ● {stateLabel[state]}
        </span>
      </div>
      <div style={{ display: 'flex', gap: 12, color: TOKENS.textFaint, fontSize: 10 }}>
        <span>{runs} runs</span>
        <span>{usedMb}MB</span>
        <span>libre {freeStr}</span>
        {data.fw_version && <span>fw {data.fw_version}</span>}
      </div>
    </div>);

}

// ---------- LAYOUT: COCKPIT (default) ----------
// Monitor-only run screen. Big distance + time + HR on top; read-only
// pace / cadence / incline below. In-run adjusters removed — during a run
// you only watch. Cadence is shown in FULL steps/min: the same number Garmin
// displays (the BLE RSC byte travels halved and Garmin doubles it back).
function CockpitLayout({ session }) {
  const dist = fmtDist(session.distance);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 10, padding: 12, paddingTop: 10, flex: 1 }}>
      {/* Top row — hero metrics. Distance trimmed a touch to give TEMPS more
         room (HH:MM:SS was cramped); HR column width unchanged. */}
      <div style={{ display: 'grid', gridTemplateColumns: '1.3fr 1.2fr 1fr', gap: 10, height: 150 }}>
        <Panel style={{ display: 'flex', flexDirection: 'column', overflow: 'hidden', minWidth: 0 }}>
          <Label color={TOKENS.textDim}>DISTANCE</Label>
          <div style={{ flex: 1, display: 'flex', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 10, flexWrap: 'nowrap', minWidth: 0 }}>
              <div style={{
                fontSize: 52, fontWeight: 800, lineHeight: 0.9,
                color: TOKENS.text, fontVariantNumeric: 'tabular-nums',
                flexShrink: 0
              }}>{dist.val}</div>
              <div style={{ fontSize: 18, color: TOKENS.textDim, fontWeight: 600, flexShrink: 0 }}>{dist.unit}</div>
            </div>
          </div>
        </Panel>

        <Panel style={{ display: 'flex', flexDirection: 'column' }}>
          <Label color={TOKENS.textDim}>TEMPS</Label>
          <div style={{ flex: 1, display: 'flex', alignItems: 'center' }}>
            <div style={{
              fontSize: 56, fontWeight: 800, lineHeight: 0.9,
              color: TOKENS.text, fontVariantNumeric: 'tabular-nums', letterSpacing: -2
            }}>{fmtTime(session.elapsedSec)}</div>
          </div>
        </Panel>

        <Panel style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between', position: 'relative' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Label color={TOKENS.textDim}>HR</Label>
            <div style={{
              fontSize: 10, fontWeight: 800, padding: '2px 6px', borderRadius: 4,
              background: `${hrColor(session.hr)}22`, color: hrColor(session.hr), letterSpacing: 1, height: "16px"
            }}>{hrZone(session.hr)}</div>
          </div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <div style={{
              fontSize: 56, fontWeight: 800, lineHeight: 0.9,
              color: hrColor(session.hr), fontVariantNumeric: 'tabular-nums', letterSpacing: -2
            }}>{session.hr || '--'}</div>
            <div style={{ fontSize: 18, color: TOKENS.textDim, fontWeight: 600 }}>bpm</div>
          </div>
          <div style={{ display: 'flex', gap: 2, height: 4, borderRadius: 2, overflow: 'hidden' }}>
            {HR_ZONES.slice(0, 5).map((z, i) =>
            <div key={i} style={{
              flex: 1,
              background: z.color,
              opacity: hrZone(session.hr) === z.label ? 1 : 0.18
            }} />
            )}
          </div>
        </Panel>
      </div>

      {/* Bottom row — read-only pace / cadence / incline (monitor only) */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10, flex: 1 }}>
        <Panel style={{ display: 'flex', flexDirection: 'column', padding: 14 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <Label>PACE</Label>
            <span style={{ fontSize: 11, color: TOKENS.amber, fontWeight: 700 }}>x{session.paceMult.toFixed(2)}</span>
          </div>
          <div style={{ flex: 1, display: 'flex', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <div style={{
                fontSize: 64, fontWeight: 800, color: TOKENS.pace,
                fontVariantNumeric: 'tabular-nums', lineHeight: 0.9
              }}>{fmtPace(paceFromSpeed(session.speed))}</div>
              <div style={{ fontSize: 15, color: TOKENS.textDim, fontWeight: 600 }}>min/km</div>
            </div>
          </div>
          <div style={{ fontSize: 15, color: TOKENS.textFaint }}>&nbsp;</div>
        </Panel>

        <Panel style={{ display: 'flex', flexDirection: 'column', padding: 14 }}>
          <Label>CADENCE</Label>
          <div style={{ flex: 1, display: 'flex', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <div style={{
                fontSize: 64, fontWeight: 800, color: TOKENS.cadence,
                fontVariantNumeric: 'tabular-nums', lineHeight: 0.9
              }}>{session.cadence || '--'}</div>
              <div style={{ fontSize: 15, color: TOKENS.textDim, fontWeight: 600 }}>spm</div>
            </div>
          </div>
          <div style={{ fontSize: 15, color: TOKENS.textFaint }}>pas par minute</div>
        </Panel>

        <Panel style={{ display: 'flex', flexDirection: 'column', padding: 14 }}>
          <Label>INCLINAISON</Label>
          <div style={{ flex: 1, display: 'flex', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
              <div style={{
                fontSize: 64, fontWeight: 800, color: TOKENS.incline,
                fontVariantNumeric: 'tabular-nums', lineHeight: 0.9
              }}>{session.incline.toFixed(1)}</div>
              <div style={{ fontSize: 15, color: TOKENS.textDim, fontWeight: 600 }}>%</div>
            </div>
          </div>
          <div style={{ fontSize: 15, color: TOKENS.textFaint }}>
            D+ <span style={{ fontSize: 17, color: TOKENS.elev, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>{Math.round(session.elevGain)}m</span>
          </div>
        </Panel>
      </div>
    </div>);

}


Object.assign(window, { CockpitLayout, StatusBar });