// Mode selector + start screen, save flow with summary, calibration page.

const { useState: useStateS } = React;

// ---------- IDLE / START SCREEN ----------
// Mode is chosen HERE, before starting. Big visual difference between PISTE and RANDOM.
function IdleScreen({ mode, setMode, onStart, onCalibration, ble, setBle, lastRun, liveSpeed, liveIncline, liveHR,
                     routeCount = 0, routeMinKm = 0, routeMaxKm = 0 }) {
  // Random-pool description built from the live catalogue (GPX dropped into
  // ~/treadmill_routes on the Pi), so it never goes stale as routes change.
  const fmtKm = (v) => (Number(v) % 1 === 0 ? String(v) : Number(v).toFixed(1)).replace('.', ',');
  const randomDetail = routeCount > 0
    ? `Tiré dans ${routeCount} parcours · ${fmtKm(routeMinKm)}–${fmtKm(routeMaxKm)} km`
    : 'Parcours tiré au hasard · sans répétition';
  const livePace = liveSpeed > 0.3
    ? `${Math.floor(60/liveSpeed)}:${String(Math.round((60/liveSpeed % 1)*60)).padStart(2,'0')}`
    : '--:--';
  const hrOk = liveHR > 0;
  const speedOk = liveSpeed > 0.1;
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: 16, gap: 14 }}>
      {/* Mode picker — front and center */}
      <div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8, gap: 12, whiteSpace: 'nowrap' }}>
          <Label>MODE DE PARCOURS</Label>
          <span style={{ fontSize: 11, color: TOKENS.textFaint }}>Choisis avant de démarrer</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10 }}>
          <ModeCard
            active={mode === 'piste'}
            onClick={() => setMode('piste')}
            color={TOKENS.modePiste}
            title="PISTE"
            sub="Parcours fixe"
            detail="Boucle 400 m · piste · à plat"
          />
          <ModeCard
            active={mode === 'random'}
            onClick={() => setMode('random')}
            color={TOKENS.modeRandom}
            title="RANDOM"
            sub="Parcours aléatoire"
            detail={randomDetail}
          />
        </div>
      </div>

      {/* Start button — huge */}
      <div style={{ display: 'flex', gap: 10, alignItems: 'stretch', flexShrink: 0 }}>
        <Btn color="green" size="xl" onClick={onStart} style={{ flex: 1, height: 84, fontSize: 32, letterSpacing: 4, flexDirection: 'row', whiteSpace: 'nowrap' }}>
          <span>▶ START</span>
        </Btn>
        <Btn color={ble ? 'green' : 'neutral'} size="xl" onClick={() => setBle(!ble)} style={{ width: 130, height: 84, flexDirection: 'column', gap: 2 }}>
          <span style={{ fontSize: 11, opacity: 0.85, letterSpacing: 1 }}>BLE</span>
          <span style={{ fontSize: 22 }}>{ble ? 'ON' : 'OFF'}</span>
        </Btn>
        <Btn color="ghost" size="xl" onClick={onCalibration} style={{ width: 130, height: 84, flexDirection: 'column', gap: 2 }}>
          <span style={{ fontSize: 16, opacity: 0.85 }}>⚙</span>
          <span style={{ fontSize: 14, letterSpacing: 1 }}>CALIBRATION</span>
        </Btn>
      </div>

      {/* Live sensor check — verify everything works before starting */}
      <Panel style={{ padding: 12, display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ display: 'flex', flexDirection: 'column', flexShrink: 0 }}>
          <Label>CAPTEURS LIVE</Label>
          <div style={{ fontSize: 10, color: TOKENS.textFaint, marginTop: 2 }}>vérifie avant de démarrer</div>
        </div>
        <div style={{ display: 'flex', gap: 14, marginLeft: 'auto', alignItems: 'baseline' }}>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 10, color: TOKENS.textFaint, letterSpacing: 1, fontWeight: 700 }}>PACE</div>
            <div style={{ fontSize: 22, fontWeight: 800, color: speedOk ? TOKENS.pace : TOKENS.textFaint, fontVariantNumeric: 'tabular-nums' }}>
              {livePace}<span style={{ fontSize: 11, color: TOKENS.textDim, fontWeight: 600, marginLeft: 3 }}>min/km</span>
            </div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 10, color: TOKENS.textFaint, letterSpacing: 1, fontWeight: 700 }}>INCL</div>
            <div style={{ fontSize: 22, fontWeight: 800, color: TOKENS.incline, fontVariantNumeric: 'tabular-nums' }}>
              {liveIncline >= 0 ? '+' : ''}{liveIncline.toFixed(1)}<span style={{ fontSize: 11, color: TOKENS.textDim, fontWeight: 600, marginLeft: 3 }}>%</span>
            </div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 10, color: TOKENS.textFaint, letterSpacing: 1, fontWeight: 700, display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 4 }}>
              <Dot color={hrOk ? TOKENS.green : TOKENS.red} pulse={hrOk} /> HR
            </div>
            <div style={{ fontSize: 22, fontWeight: 800, color: hrOk ? TOKENS.hr : TOKENS.textFaint, fontVariantNumeric: 'tabular-nums' }}>
              {hrOk ? liveHR : '--'}<span style={{ fontSize: 11, color: TOKENS.textDim, fontWeight: 600, marginLeft: 3 }}>bpm</span>
            </div>
          </div>
        </div>
      </Panel>

      {/* Last run reminder */}
      <Panel style={{ padding: 12, display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <Label>DERNIÈRE ACTIVITÉ</Label>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 14, color: TOKENS.text, fontWeight: 600, marginTop: 6, whiteSpace: 'nowrap' }}>
            <span>{lastRun.date}</span>
            <ModeBadge mode={lastRun.mode} compact />
          </div>
        </div>
        <div style={{ display: 'flex', gap: 18 }}>
          <SmallStat label="Dist" value={lastRun.dist} unit="km" />
          <SmallStat label="Temps" value={lastRun.time} />
          <SmallStat label="Pace" value={lastRun.pace} />
          <SmallStat label="D+" value={lastRun.elev} unit="m" />
        </div>
      </Panel>
    </div>
  );
}

function ModeCard({ active, onClick, color, title, sub, detail }) {
  return (
    <div
      onClick={onClick}
      style={{
        padding: 14,
        borderRadius: 14,
        background: active ? `${color}1a` : TOKENS.bgPanel,
        border: `2px solid ${active ? color : TOKENS.border}`,
        cursor: 'pointer',
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
        position: 'relative',
        transition: 'all 0.12s',
        minHeight: 110,
      }}
    >
      <div style={{
        position: 'absolute', top: 14, right: 14, width: 16, height: 16, borderRadius: '50%',
        border: `2px solid ${active ? color : TOKENS.borderStrong}`,
        background: active ? color : 'transparent',
        boxShadow: active ? `0 0 0 4px ${color}33` : 'none',
      }} />
      <div style={{ fontSize: 28, fontWeight: 800, color: active ? color : TOKENS.text, letterSpacing: 2 }}>{title}</div>
      <div style={{ fontSize: 13, color: active ? color : TOKENS.textDim, fontWeight: 600 }}>{sub}</div>
      <div style={{ fontSize: 11, color: TOKENS.textFaint, marginTop: 'auto' }}>{detail}</div>
    </div>
  );
}

function SmallStat({ label, value, unit = '' }) {
  return (
    <div style={{ textAlign: 'right' }}>
      <div style={{ fontSize: 10, color: TOKENS.textFaint, letterSpacing: 1, fontWeight: 700 }}>{label.toUpperCase()}</div>
      <div style={{ fontSize: 18, color: TOKENS.text, fontWeight: 800, fontVariantNumeric: 'tabular-nums' }}>
        {value}{unit && <span style={{ fontSize: 12, color: TOKENS.textDim, fontWeight: 600, marginLeft: 2 }}>{unit}</span>}
      </div>
    </div>
  );
}

// ---------- HEADER (during RUNNING / PAUSED) ----------
// Mode badge always visible. Includes pause button.
function RunHeader({ state, mode, paceMult, onPause, onResume, onStop }) {
  return (
    <div style={{
      height: 56,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      padding: '0 14px',
      background: TOKENS.bgInset,
      borderBottom: `1px solid ${TOKENS.border}`,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flexShrink: 1 }}>
        <ModeBadge mode={mode} compact />
        {state === 'PAUSED' && (
          <span style={{ fontSize: 12, fontWeight: 800, color: TOKENS.amber, letterSpacing: 1, whiteSpace: 'nowrap' }}>
            ⏸ AUTO-PAUSE
          </span>
        )}
        {state === 'RUNNING' && (
          <span style={{ fontSize: 12, fontWeight: 800, color: TOKENS.green, letterSpacing: 1, display: 'flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
            <Dot color={TOKENS.red} pulse /> REC
          </span>
        )}
      </div>
      <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
        {state === 'RUNNING' && (
          <Btn color="amber" size="md" onClick={onPause} style={{ minWidth: 100, whiteSpace: 'nowrap' }}>⏸ PAUSE</Btn>
        )}
        {state === 'PAUSED' && (
          <>
            <Btn color="blue" size="md" onClick={onResume} style={{ minWidth: 100, whiteSpace: 'nowrap' }}>▶ RESUME</Btn>
            <Btn color="red" size="md" onClick={onStop} style={{ minWidth: 90, whiteSpace: 'nowrap' }}>■ STOP</Btn>
          </>
        )}
      </div>
    </div>
  );
}

// ---------- SAVE / SUMMARY SCREEN ----------
// Replaces the confusing PISTE/SAVE/ANNULER row with a clear summary.
function SaveScreen({ session, mode, onSave, onDiscard, onResume }) {
  const dist = fmtDist(session.distance);
  // distance is in metres, elapsedSec in seconds -> m/s * 3.6 = km/h.
  // (was * 3600, which yielded a nonsense speed and a 0:00 pace.)
  const avgPace = fmtPace(paceFromSpeed(session.distance / Math.max(1, session.elapsedSec) * 3.6));
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', padding: 14, gap: 12 }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <div>
          <div style={{ fontSize: 24, fontWeight: 800, color: TOKENS.text, letterSpacing: 1 }}>RÉSUMÉ DE L'ACTIVITÉ</div>
          <div style={{ fontSize: 12, color: TOKENS.textDim, marginTop: 2 }}>Vérifie avant de sauvegarder</div>
        </div>
        <ModeBadge mode={mode} />
      </div>

      {/* Big stats grid — top row larger, bottom row tighter */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gridTemplateRows: '1.4fr 1fr', gap: 10, flex: 1 }}>
        <Panel style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between', padding: 14 }}>
          <Label>DISTANCE</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <div style={{ fontSize: 72, fontWeight: 800, color: TOKENS.text, lineHeight: 0.9, fontVariantNumeric: 'tabular-nums' }}>{dist.val}</div>
            <div style={{ fontSize: 20, color: TOKENS.textDim, fontWeight: 600 }}>{dist.unit}</div>
          </div>
          <div style={{ fontSize: 11, color: TOKENS.textFaint }}>parcourus</div>
        </Panel>
        <Panel style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between', padding: 14 }}>
          <Label>TEMPS</Label>
          <div style={{ fontSize: 64, fontWeight: 800, color: TOKENS.text, lineHeight: 0.9, fontVariantNumeric: 'tabular-nums' }}>
            {fmtTime(session.elapsedSec)}
          </div>
          <div style={{ fontSize: 11, color: TOKENS.textFaint }}>en mouvement</div>
        </Panel>
        <Panel style={{ display: 'flex', flexDirection: 'column', justifyContent: 'space-between', padding: 14 }}>
          <Label>PACE MOYEN</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <div style={{ fontSize: 64, fontWeight: 800, color: TOKENS.pace, lineHeight: 0.9, fontVariantNumeric: 'tabular-nums' }}>{avgPace}</div>
            <div style={{ fontSize: 14, color: TOKENS.textDim }}>min/km</div>
          </div>
          <div style={{ fontSize: 11, color: TOKENS.textFaint }}>{(session.distance / Math.max(1, session.elapsedSec) * 3.6).toFixed(1)} km/h moy</div>
        </Panel>

        <Panel style={{ padding: 12, display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <Label>D+ ÉLÉVATION</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
            <div style={{ fontSize: 44, fontWeight: 800, color: TOKENS.elev, fontVariantNumeric: 'tabular-nums', lineHeight: 0.9 }}>{Math.round(session.elevGain)}</div>
            <div style={{ fontSize: 16, color: TOKENS.textDim, fontWeight: 600 }}>m</div>
          </div>
        </Panel>
        <Panel style={{ padding: 12, display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <Label>HR MOY · MAX</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
            <div style={{ fontSize: 36, fontWeight: 800, color: TOKENS.hr, fontVariantNumeric: 'tabular-nums', lineHeight: 0.9 }}>
              {session.hrAvg ? session.hrAvg : '--'}
            </div>
            <div style={{ fontSize: 18, color: TOKENS.textFaint, fontWeight: 600 }}>·</div>
            <div style={{ fontSize: 36, fontWeight: 800, color: TOKENS.hr, fontVariantNumeric: 'tabular-nums', lineHeight: 0.9, opacity: 0.85 }}>
              {session.hrMax ? session.hrMax : '--'}
            </div>
            <div style={{ fontSize: 13, color: TOKENS.textDim, fontWeight: 600, marginLeft: 4 }}>bpm</div>
          </div>
        </Panel>
        <Panel style={{ padding: 12, display: 'flex', flexDirection: 'column', justifyContent: 'space-between' }}>
          <Label>CADENCE MOY</Label>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 4 }}>
            <div style={{ fontSize: 44, fontWeight: 800, color: TOKENS.cadence, fontVariantNumeric: 'tabular-nums', lineHeight: 0.9 }}>{session.cadence}</div>
            <div style={{ fontSize: 14, color: TOKENS.textDim, fontWeight: 600 }}>spm</div>
          </div>
        </Panel>
      </div>

      {/* Action row — clear hierarchy */}
      <div style={{ display: 'flex', gap: 10 }}>
        <Btn color="ghost" size="lg" onClick={onResume} style={{ flex: 1 }}>↩ REPRENDRE</Btn>
        <Btn color="red" size="lg" onClick={onDiscard} style={{ flex: 1 }}>🗑 ANNULER</Btn>
        <Btn color="green" size="lg" onClick={onSave} style={{ flex: 2 }}>✓ SAUVEGARDER</Btn>
      </div>
    </div>
  );
}

// ---------- CALIBRATION PAGE ----------
// Tabs: VITESSE | INCLINAISON. Incline calibration is multi-point: the
// user captures the raw measured value at -3/0/3/6/9/12 %. Six integer
// targets give finer piecewise interpolation across the treadmill's full
// -3..15% range. Pi config keeps all 6 captures; the firmware only mirrors
// 0 and 12 (TOF_CAL_POINT0/12) — the 3/6/9 points live only Pi-side.
const INCLINE_TARGETS = [-3, 0, 3, 6, 9, 12];

function CalibrationPage({
  onBack, liveSpeed, liveIncline,
  refSpeed = 5.0, paceFactor = 1.0,
  inclinePoints = {}, calibState = 'idle', calibProgress = 0,
  onRefSpeedDelta = () => {}, onCalibrateSpeed = () => {}, onResetSpeed = () => {},
  onCaptureInclinePoint = () => {}, onClearInclinePoints = () => {},
  onResetIncline = () => {}, onInclineOffsetDelta = () => {},
  cadDynamic = 0, cadSpm = 0, cadSteps = 0, imuOk = false,
  cadDetConf = 0,
  cadThreshold = 2.0, cadRefractMs = 180,
  onCadThresholdDelta = () => {}, onCadRefractDelta = () => {},
  cadRecording = false, cadRawLines = 0,
  onCadRecordStart = () => {}, onCadRecordStop = () => {},
}) {
  const [tab, setTab] = useStateS('speed');
  const paceMult = paceFactor;
  // Normalise keys: server emits "-3.0", "0.0"... — strip ".0" for lookup.
  const points = {};
  Object.keys(inclinePoints || {}).forEach(k => {
    const t = parseFloat(k);
    points[String(t)] = inclinePoints[k];
  });
  const captureCount = Object.keys(points).length;

  const captureAt = (target) => onCaptureInclinePoint(target);
  const clearAll = () => onClearInclinePoints();

  // Cadence signal bar geometry: peak impact vs threshold on a common scale.
  const cadScale = Math.max(6, cadThreshold * 2.5, cadDynamic * 1.1);
  const cadDynPct = Math.max(0, Math.min(1, cadDynamic / cadScale)) * 100;
  const cadThrPct = Math.max(0, Math.min(1, cadThreshold / cadScale)) * 100;
  const cadHit = cadDynamic >= cadThreshold;
  // Pi detector lock: confidence = ACF peak height. >=0.5 locked, 0.2-0.5 searching.
  const cadLocked = cadDetConf >= 0.5;
  const cadLockColor = cadLocked ? TOKENS.green : cadDetConf >= 0.2 ? TOKENS.amber : TOKENS.textFaint;
  const cadLockLabel = cadLocked ? 'VERROUILLÉ' : cadDetConf >= 0.2 ? 'RECHERCHE' : 'INACTIF';

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <div style={{ height: 56, display: 'flex', alignItems: 'center', gap: 12, padding: '0 14px', background: TOKENS.bgInset, borderBottom: `1px solid ${TOKENS.border}` }}>
        <Btn color="ghost" size="md" onClick={onBack}>← RETOUR</Btn>
        <div style={{ fontSize: 22, fontWeight: 800, color: TOKENS.text, letterSpacing: 2 }}>CALIBRATION</div>
        {/* Tabs */}
        <div style={{ marginLeft: 'auto', display: 'flex', gap: 6, background: TOKENS.bgPanel, padding: 4, borderRadius: 10, border: `1px solid ${TOKENS.border}` }}>
          <TabBtn active={tab === 'speed'} onClick={() => setTab('speed')}>VITESSE</TabBtn>
          <TabBtn active={tab === 'incline'} onClick={() => setTab('incline')}>INCLINAISON</TabBtn>
          <TabBtn active={tab === 'cadence'} onClick={() => setTab('cadence')}>CADENCE</TabBtn>
        </div>
      </div>

      <div style={{ flex: 1, padding: 14, display: 'flex', flexDirection: 'column', gap: 10, minHeight: 0 }}>
        {tab === 'speed' && (
          <Panel style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 12, flex: 1 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <Label color={TOKENS.green}>RÉFÉRENCE</Label>
              <span style={{ fontSize: 13, color: TOKENS.textDim }}>
                Brute · <span style={{ color: TOKENS.text, fontWeight: 700, fontVariantNumeric: 'tabular-nums' }}>{liveSpeed.toFixed(1)}</span>
                <span style={{ color: TOKENS.textFaint }}> km/h</span>
              </span>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 16, background: TOKENS.bgInset, borderRadius: 12, padding: '14px 18px' }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                <div style={{ fontSize: 84, fontWeight: 800, color: TOKENS.text, fontVariantNumeric: 'tabular-nums', lineHeight: 0.9 }}>{refSpeed.toFixed(1)}</div>
                <span style={{ fontSize: 18, color: TOKENS.textDim, fontWeight: 600 }}>km/h</span>
              </div>
              <div style={{ display: 'flex', gap: 10, marginLeft: 'auto' }}>
                <Btn color="red" size="lg" onClick={() => onRefSpeedDelta(-0.1)} style={{ width: 80, height: 80, fontSize: 38, padding: 0 }}>−</Btn>
                <Btn color="green" size="lg" onClick={() => onRefSpeedDelta(+0.1)} style={{ width: 80, height: 80, fontSize: 38, padding: 0 }}>+</Btn>
              </div>
            </div>
            <div style={{ fontSize: 12, color: TOKENS.textDim, lineHeight: 1.5 }}>
              Règle le tapis sur <b style={{ color: TOKENS.text }}>{refSpeed.toFixed(1)} km/h</b> exactement,
              puis appuie sur <b style={{ color: TOKENS.green }}>CALIBRER 10s</b>.
              Le facteur sera mémorisé automatiquement.
            </div>
            <div style={{ display: 'flex', gap: 10, marginTop: 'auto' }}>
              <Btn color="green" size="lg" wide style={{ flex: 2 }} onClick={onCalibrateSpeed}>
                {calibState === 'recording' ? `MESURE… ${Math.round(calibProgress * 10)}s` : 'CALIBRER 10s'}
              </Btn>
              <Btn color="amber" size="lg" style={{ flex: 1 }} onClick={onResetSpeed}>RESET</Btn>
            </div>
            <div style={{ fontSize: 11, color: TOKENS.textFaint, fontVariantNumeric: 'tabular-nums' }}>
              Facteur actuel · ×{paceMult.toFixed(3)}
            </div>
          </Panel>
        )}

        {tab === 'incline' && (
          <Panel style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 10, flex: 1, minHeight: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <Label color={TOKENS.blue}>POINTS DE CALIBRATION</Label>
                <div style={{ fontSize: 11, color: TOKENS.textFaint, marginTop: 2 }}>
                  Mémorise la valeur brute mesurée à chaque pente cible — interpolation linéaire entre les points
                </div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div style={{ fontSize: 10, color: TOKENS.textFaint, letterSpacing: 1.5, fontWeight: 700 }}>LIVE BRUT</div>
                <div style={{ fontSize: 24, fontWeight: 800, color: TOKENS.incline, fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>
                  {liveIncline >= 0 ? '+' : ''}{liveIncline.toFixed(2)}<span style={{ fontSize: 12, color: TOKENS.textDim, marginLeft: 2 }}>%</span>
                </div>
              </div>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, flex: 1, minHeight: 0 }}>
              {INCLINE_TARGETS.map(target => {
                const captured = points[String(target)];
                const has = captured !== undefined;
                const drift = has ? (captured - target) : 0;
                return (
                  <div key={target} style={{
                    display: 'flex', alignItems: 'center', gap: 12, flex: 1, minHeight: 0,
                    background: TOKENS.bgInset, borderRadius: 8,
                    padding: '0 12px',
                    border: `1px solid ${has ? TOKENS.green + '55' : TOKENS.border}`,
                  }}>
                    {/* Target value */}
                    <div style={{ minWidth: 64, fontSize: 20, fontWeight: 800, color: TOKENS.text, fontVariantNumeric: 'tabular-nums' }}>
                      {target >= 0 ? '+' : ''}{target}<span style={{ fontSize: 12, color: TOKENS.textDim, marginLeft: 2 }}>%</span>
                    </div>

                    {/* Captured raw — single line */}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      {has ? (
                        <span style={{ fontSize: 15, fontWeight: 800, color: TOKENS.green, fontVariantNumeric: 'tabular-nums' }}>
                          brut {captured >= 0 ? '+' : ''}{captured.toFixed(2)}%
                          <span style={{ fontSize: 10, color: TOKENS.textFaint, fontWeight: 600, marginLeft: 8 }}>
                            écart {drift >= 0 ? '+' : ''}{drift.toFixed(2)}
                          </span>
                        </span>
                      ) : (
                        <span style={{ fontSize: 12, color: TOKENS.textFaint, fontStyle: 'italic' }}>
                          Non mémorisé
                        </span>
                      )}
                    </div>

                    {/* Capture button */}
                    <Btn
                      color={has ? 'amber' : 'green'}
                      size="sm"
                      onClick={() => captureAt(target)}
                      style={{ minWidth: 120, height: 32, fontSize: 11 }}
                    >
                      {has ? 'REMPLACER' : 'MÉMORISER'}
                    </Btn>
                  </div>
                );
              })}
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', paddingTop: 4 }}>
              <span style={{ fontSize: 11, color: TOKENS.textFaint, fontVariantNumeric: 'tabular-nums' }}>
                {captureCount} points capturés · {captureCount >= 2 ? 'correction active' : 'min. 2 points'}
              </span>
              <Btn color="red" size="sm" onClick={clearAll} style={{ minWidth: 130 }}>
                EFFACER TOUT
              </Btn>
            </div>
          </Panel>
        )}

        {tab === 'cadence' && (
          <Panel style={{ padding: 14, display: 'flex', flexDirection: 'column', gap: 12, flex: 1, minHeight: 0 }}>
            {/* Header: measured cadence + IMU health */}
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <Label color={TOKENS.cadence}>CADENCE MESURÉE</Label>
                <div style={{ fontSize: 11, color: TOKENS.textFaint, marginTop: 2 }}>
                  Détecteur Pi (autocorrélation) · repli capteur Feather
                </div>
              </div>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
                <div style={{ fontSize: 40, fontWeight: 800, color: cadSpm > 0 ? TOKENS.cadence : TOKENS.textFaint, fontVariantNumeric: 'tabular-nums', lineHeight: 0.9 }}>
                  {cadSpm > 0 ? cadSpm : '--'}
                </div>
                <span style={{ fontSize: 13, color: TOKENS.textDim, fontWeight: 600 }}>spm</span>
              </div>
            </div>

            {!imuOk && (
              <div style={{ fontSize: 12, color: TOKENS.red, fontWeight: 700, background: TOKENS.red + '1a', borderRadius: 8, padding: '8px 12px' }}>
                ⚠ Accéléromètre non détecté (imu=FAIL) — vérifie le câblage I2C du LSM303
              </div>
            )}

            {/* Pi detector lock: watch this reach "VERROUILLÉ" while running */}
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
                <span style={{ fontSize: 10, color: TOKENS.textFaint, letterSpacing: 1.5, fontWeight: 700 }}>VERROUILLAGE DÉTECTEUR</span>
                <span style={{ fontSize: 13, fontWeight: 800, color: cadLockColor, fontVariantNumeric: 'tabular-nums' }}>
                  {cadLockLabel} <span style={{ fontSize: 11, color: TOKENS.textFaint }}>{cadDetConf.toFixed(2)}</span>
                </span>
              </div>
              <div style={{ position: 'relative', height: 14, background: TOKENS.bgInset, borderRadius: 6, overflow: 'hidden', border: `1px solid ${TOKENS.border}` }}>
                <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: `${Math.max(0, Math.min(1, cadDetConf)) * 100}%`, background: cadLockColor, opacity: 0.7, transition: 'width 0.2s' }} />
                {/* lock threshold marker at 0.5 */}
                <div style={{ position: 'absolute', left: '50%', top: -2, bottom: -2, width: 2, background: TOKENS.green }} />
              </div>
              <div style={{ fontSize: 10, color: TOKENS.textFaint, marginTop: 4 }}>
                Confiance du détecteur Pi. Repère <span style={{ color: TOKENS.green, fontWeight: 700 }}>vert</span> = seuil de verrouillage (0,50). Au-dessus = rythme capté ; en dessous, repli sur l'estimation vitesse.
              </div>
            </div>

            {/* Live impact signal vs threshold */}
            <div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 6 }}>
                <span style={{ fontSize: 10, color: TOKENS.textFaint, letterSpacing: 1.5, fontWeight: 700 }}>SIGNAL D'IMPACT (pic) · REPLI FEATHER</span>
                <span style={{ fontSize: 13, fontWeight: 800, color: cadHit ? TOKENS.green : TOKENS.textDim, fontVariantNumeric: 'tabular-nums' }}>
                  {cadDynamic.toFixed(1)} <span style={{ fontSize: 10, color: TOKENS.textFaint }}>m/s²</span>
                </span>
              </div>
              <div style={{ position: 'relative', height: 26, background: TOKENS.bgInset, borderRadius: 6, overflow: 'hidden', border: `1px solid ${TOKENS.border}` }}>
                {/* fill = current peak */}
                <div style={{ position: 'absolute', left: 0, top: 0, bottom: 0, width: `${cadDynPct}%`, background: cadHit ? TOKENS.green : TOKENS.blue, opacity: 0.65, transition: 'width 0.15s' }} />
                {/* threshold marker */}
                <div style={{ position: 'absolute', left: `${cadThrPct}%`, top: -2, bottom: -2, width: 2, background: TOKENS.amber }} />
              </div>
              <div style={{ fontSize: 10, color: TOKENS.textFaint, marginTop: 4 }}>
                Barre = pic d'impact · ligne <span style={{ color: TOKENS.amber, fontWeight: 700 }}>orange</span> = seuil. Règle le seuil juste au-dessus du bruit au repos, sous les pics de pas.
              </div>
            </div>

            {/* Threshold control */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 12, background: TOKENS.bgInset, borderRadius: 10, padding: '10px 14px' }}>
              <div style={{ minWidth: 120 }}>
                <span style={{ fontSize: 9, color: TOKENS.textFaint, letterSpacing: 1.5, fontWeight: 700 }}>SEUIL</span>
                <div style={{ fontSize: 30, fontWeight: 800, color: TOKENS.text, fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>
                  {cadThreshold.toFixed(2)}
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8, marginLeft: 'auto' }}>
                <Btn color="red" size="md" onClick={() => onCadThresholdDelta(-0.25)} style={{ width: 60, height: 56, fontSize: 28, padding: 0 }}>−</Btn>
                <Btn color="green" size="md" onClick={() => onCadThresholdDelta(+0.25)} style={{ width: 60, height: 56, fontSize: 28, padding: 0 }}>+</Btn>
              </div>
            </div>

            {/* Refractory + steps */}
            <div style={{ display: 'flex', gap: 10 }}>
              <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 10, background: TOKENS.bgInset, borderRadius: 10, padding: '8px 12px' }}>
                <div style={{ minWidth: 0 }}>
                  <span style={{ fontSize: 9, color: TOKENS.textFaint, letterSpacing: 1.5, fontWeight: 700 }}>ANTI-REBOND</span>
                  <div style={{ fontSize: 20, fontWeight: 800, color: TOKENS.text, fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>
                    {cadRefractMs}<span style={{ fontSize: 11, color: TOKENS.textDim, marginLeft: 2 }}>ms</span>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, marginLeft: 'auto' }}>
                  <Btn color="ghost" size="sm" onClick={() => onCadRefractDelta(-20)} style={{ width: 44, height: 40, fontSize: 20, padding: 0 }}>−</Btn>
                  <Btn color="ghost" size="sm" onClick={() => onCadRefractDelta(+20)} style={{ width: 44, height: 40, fontSize: 20, padding: 0 }}>+</Btn>
                </div>
              </div>
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', background: TOKENS.bgInset, borderRadius: 10, padding: '8px 12px' }}>
                <span style={{ fontSize: 9, color: TOKENS.textFaint, letterSpacing: 1.5, fontWeight: 700 }}>PAS DÉTECTÉS</span>
                <div style={{ fontSize: 20, fontWeight: 800, color: TOKENS.cadence, fontVariantNumeric: 'tabular-nums', lineHeight: 1 }}>
                  {cadSteps}
                </div>
              </div>
            </div>

            {/* Raw recording for offline calibration */}
            <div style={{ marginTop: 'auto', display: 'flex', alignItems: 'center', gap: 12, background: TOKENS.bgInset, borderRadius: 10, padding: '10px 14px', border: `1px solid ${cadRecording ? TOKENS.red + '99' : TOKENS.border}` }}>
              <div style={{ minWidth: 0, flex: 1 }}>
                <span style={{ fontSize: 9, color: TOKENS.textFaint, letterSpacing: 1.5, fontWeight: 700 }}>ENREGISTREMENT BRUT</span>
                <div style={{ fontSize: 12, color: cadRecording ? TOKENS.red : TOKENS.textDim, fontWeight: 600, marginTop: 2 }}>
                  {cadRecording
                    ? <span><Dot color={TOKENS.red} pulse /> Enregistre… {cadRawLines.toLocaleString()} lignes</span>
                    : 'Capture 100 Hz pour calibration hors-ligne'}
                </div>
              </div>
              {cadRecording
                ? <Btn color="red" size="md" onClick={onCadRecordStop} style={{ minWidth: 140, height: 48 }}>■ ARRÊTER</Btn>
                : <Btn color="green" size="md" onClick={onCadRecordStart} style={{ minWidth: 140, height: 48 }}>● ENREGISTRER</Btn>}
            </div>

            <div style={{ fontSize: 11, color: TOKENS.textDim, lineHeight: 1.5 }}>
              Cours à allure normale : la cadence doit coller à ton ressenti. Si elle reste
              à 0 → baisse le seuil. Si elle est trop haute (compte double) → monte le seuil
              ou augmente l'anti-rebond. Réglages sauvegardés automatiquement.
            </div>
          </Panel>
        )}
      </div>
    </div>
  );
}

function TabBtn({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 16px',
        borderRadius: 6,
        background: active ? TOKENS.green : 'transparent',
        color: active ? '#0a0a0b' : TOKENS.text,
        border: 'none',
        cursor: 'pointer',
        fontSize: 12,
        fontWeight: 800,
        letterSpacing: 1.5,
      }}
    >
      {children}
    </button>
  );
}

Object.assign(window, { IdleScreen, RunHeader, SaveScreen, CalibrationPage });
