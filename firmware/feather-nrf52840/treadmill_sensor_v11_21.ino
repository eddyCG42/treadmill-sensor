// Treadmill Sensor v12.0
// ---------------------------------------------------------------------------
// GOAL OF v12: make the Feather a THIN, self-configuring I/O layer so it
// almost never needs re-flashing (it lives sealed inside the treadmill cover).
// Everything that used to be a compile-time constant or firmware-side policy
// is now a runtime parameter the Pi pushes over serial and can change by
// editing Python. What's new vs v11_21:
//
//   - Generic runtime config: `SET <key> <value>` + `GET <key>` + `CFG_DUMP`.
//     ~16 tunables (wheel diameter, debounce, timeouts, smoothing alphas,
//     cadence thresholds, ToF fallback calibration...) are now settable live.
//     The old per-parameter commands (PACE_FACTOR:, CAD_THRESH:, TOF_CAL_*...)
//     are gone — the Pi drives everything through SET.
//   - Verified sync: every accepted SET echoes `[CONFIG] key=value`; the Pi
//     waits for that ack and retries. CFG_DUMP lists all params for a full
//     re-verify. `$HELLO,<version>` at boot lets the Pi log/track firmware.
//   - Incline for Garmin now comes FROM THE PI: `BLE_INCLIN:<percent>` pushes
//     the Pi's richer 6-point calibrated value at 1-2 Hz; the firmware relays
//     it verbatim to the BLE incline characteristic. If the push goes stale
//     (>2 s, e.g. Pi off), it falls back to its own 2-point mm->% mapping, so
//     a standalone Feather->Garmin still works. Garmin and the dashboard now
//     show the SAME incline.
//   - Non-blocking serial reader (was Serial.readStringUntil, which stalled
//     the 100 Hz cadence loop up to 1 s on a partial line — glitching cadence
//     exactly during the Pi's config burst).
//   - Single FW_VERSION string used in the boot banner, $HELLO and STATUS, so
//     you can always tell from the serial log what's inside the sealed cover.
//   - Speed and cadence are still computed LOCALLY (Hall + LSM303) and NOT
//     relayed from the Pi: the firmware already does these well, and relaying
//     them would only add failure modes with no accuracy gain.
//
// Hardware unchanged: Adafruit Bluefruit nRF52840 + VL53L4CD (Pololu lib) +
// LSM303 accel + DRV5023 Hall. Wire format extended (see sendSerialData).
//
// v12.2 — robustness (fixes the ~52 s dropout / frozen-pulses incidents):
//   - Serial OUTPUT never blocks. The core's USB-CDC write() spins forever
//     while the host holds the port open but stops reading, which froze the
//     whole loop (speed, BLE to the watch, command handling). Every line now
//     goes through `Out`, a line-atomic software queue drained by txPump()
//     only as fast as the USB FIFO has room; when the queue is full, whole
//     lines are dropped and counted (TXDROP in STATUS/DIAG).
//   - Hardware watchdog (nRF52 WDT, 8 s). If loop() ever stops turning (I2C
//     bus hang, anything else) the chip resets itself; the Pi re-syncs config
//     on the `$HELLO`. The boot banner reports the reset reason so a watchdog
//     reset shows up in the Pi journal.
//   - BLE connect/disconnect events are flagged in the BLE callback task and
//     printed from loop(), so only one task ever touches the serial queue.
//   No wire-format change: a v12.1 Pi works with this firmware unchanged.
// ---------------------------------------------------------------------------

#include <bluefruit.h>
#include <Wire.h>
#include <Adafruit_LSM303_Accel.h>
#include <Adafruit_Sensor.h>
#include <VL53L4CD.h>                  // Pololu VL53L4CD

#define FW_VERSION "v12.2"
#define WDT_TIMEOUT_S     8         // loop() must turn at least this often

// ---- FIXED CONFIG (compile-time; not worth a runtime knob) ----
#define HALL_PIN          A0
#define TOF_READ_MS       100
#define ACCEL_READ_MS     10        // 100 Hz, matches LSM303DLHC default ODR
#define TOF_FAST_CONFIRM  3         // consecutive same-side samples (0.3 s)
#define TOF_CAL_TARGET_HI 12        // fallback calibration upper target (%)
// VL53L4CD timing budget (ms): 10..200. 50ms = good speed/accuracy compromise.
// Inter-measurement must be >= timing budget; 100 ms = 10 Hz. Changing these
// needs a sensor re-config, so they stay compile-time.
#define TOF_TIMING_BUDGET 50
#define TOF_INTER_MEAS    100
#define TOF_I2C_TIMEOUT   500       // ms before timeoutOccurred() trips
// Plausibility window for ToF calibration values (typo guard).
#define TOF_CAL_MIN_MM    5
#define TOF_CAL_MAX_MM    2000
// How long a Pi-pushed BLE incline stays "fresh" before we fall back to the
// firmware's own mm->% mapping.
#define PI_INCLIN_TIMEOUT 5000
#define PI_CADENCE_TIMEOUT 3000   // Pi's BLE_CAD push considered fresh for this long

static inline float clampf(float v, float lo, float hi) {
  return v < lo ? lo : (v > hi ? hi : v);
}

// ==============================
// Non-blocking serial output
// ==============================
// All output goes through `Out` (a Print, so print()/println() formatting is
// unchanged). Characters are staged until '\n', then the whole line is moved
// into a ring buffer if it fits, otherwise dropped and counted — a line is
// never sent half. txPump() (called every loop) hands the USB FIFO only as many
// bytes as it can take right now, so nothing here can ever block.
// Single-task only: call from setup()/loop(), never from a BLE callback.
#define TXQ_SIZE   2048             // > a full CFG_DUMP burst (~500 B)
#define TXQ_LINE   256              // longest single line (STATUS ~150 B)

class TxQueue : public Print {
 public:
  using Print::write;
  size_t write(uint8_t c) override {
    if (_lineLen < TXQ_LINE) _line[_lineLen++] = c;
    else _lineOverflow = true;
    if (c == '\n') commit();
    return 1;
  }
  // Move queued bytes to USB without blocking.
  void pump() {
    if (!Serial) {              // no host listening: don't hoard stale lines
      _count = 0; _tail = _head;
      return;
    }
    while (_count > 0) {
      int room = Serial.availableForWrite();
      if (room <= 0) return;
      uint16_t n = _count;
      if (n > (uint16_t)room) n = (uint16_t)room;
      if (n > TXQ_SIZE - _tail) n = TXQ_SIZE - _tail;   // contiguous chunk
      size_t sent = Serial.write(_ring + _tail, n);     // fits => returns at once
      if (sent == 0) return;
      _tail = (_tail + sent) % TXQ_SIZE;
      _count -= sent;
    }
  }
  uint32_t dropped() const { return _dropped; }
  uint16_t freeBytes() const { return TXQ_SIZE - _count; }

 private:
  void commit() {
    if (_lineOverflow || _lineLen > TXQ_SIZE - _count) {
      _dropped++;
    } else {
      for (uint16_t i = 0; i < _lineLen; i++) {
        _ring[_head] = _line[i];
        _head = (_head + 1) % TXQ_SIZE;
      }
      _count += _lineLen;
    }
    _lineLen = 0;
    _lineOverflow = false;
  }
  uint8_t  _ring[TXQ_SIZE];
  uint8_t  _line[TXQ_LINE];
  uint16_t _head = 0, _tail = 0, _count = 0, _lineLen = 0;
  bool     _lineOverflow = false;
  uint32_t _dropped = 0;
};
TxQueue Out;

// ==============================
// Hardware watchdog (nRF52 WDT)
// ==============================
// Once started it cannot be stopped and it keeps counting through a soft reset.
// Keeps running while the CPU sleeps (a blocked loop sleeps), pauses under a
// debugger. If it was already started before a soft reset, the config writes
// are ignored by the hardware and we just keep feeding it.
static inline void wdtFeed() {
  NRF_WDT->RR[0] = WDT_RR_RR_Reload;
}
void wdtStart() {
  NRF_WDT->CONFIG = (WDT_CONFIG_SLEEP_Run << WDT_CONFIG_SLEEP_Pos) |
                    (WDT_CONFIG_HALT_Pause << WDT_CONFIG_HALT_Pos);
  NRF_WDT->CRV = (uint32_t)WDT_TIMEOUT_S * 32768UL - 1;
  NRF_WDT->RREN = WDT_RREN_RR0_Msk;
  NRF_WDT->TASKS_START = 1;
  wdtFeed();
}

// ---- RUNTIME CONFIG (settable via `SET <key> <value>`) ----
// Values here are power-on defaults; the Pi re-pushes the real values from
// treadmill_config.json on every connect, so a reflashed/replaced Feather
// auto-configures.
float    wheelDiamMm     = 48.0;   // roller circumference source (speed+dist)
volatile uint16_t debounceMs = 15; // Hall ISR debounce (read in ISR)
uint16_t bleUpdateMs     = 500;
uint16_t serialUpdateMs  = 500;
uint16_t speedTimeoutMs  = 3000;
float    speedEma        = 0.25;   // speed smoothing weight on the new sample
float    tofEma          = 0.10;   // ToF smoothing weight on the new median (steady)
// Incline response (v12.2). A footfall shakes the deck for a fraction of a
// second and swings back; the incline motor moves ~10 mm per % and keeps going
// the same way. So: a SHORT median removes the impact spikes, and the EMA runs
// slow (tofEma) while the median stays within tofFastMm of the smoothed value,
// but switches to tofEmaFast once it has been off by more than that, on the
// same side, for TOF_FAST_CONFIRM samples in a row (a real move).
uint8_t  tofMedN         = 9;      // median window, samples @10 Hz (odd, 1..21)
float    tofEmaFast      = 0.50;   // EMA weight while a real move is confirmed
float    tofFastMm       = 3.0;    // deviation (mm) that counts as a move (~0.3 %)
float    accelBaseEma    = 0.02;   // accel baseline tracking weight
float    cadThreshold    = 2.0;    // m/s^2 impact deviation that counts as a step
float    cadRearm        = 1.0;    // must drop below this to re-arm (hysteresis)
uint16_t cadRefractMs    = 180;    // min gap between steps (=> <= ~333 spm)
uint16_t cadTimeoutMs    = 2000;   // no impact this long => cadence decays to 0
float    paceFactor      = 1.0;
float    inclinOffset    = 0.0;    // firmware fallback offset only

// ---- OBJETS ----
Adafruit_LSM303_Accel_Unified accel = Adafruit_LSM303_Accel_Unified(54321);
VL53L4CD sensor;                    // Pololu constructor: no args (addr 0x29)

// ---- BLE ----
BLEService rscService = BLEService(0x1814);
BLECharacteristic rscMeasurement = BLECharacteristic(0x2A53);
BLECharacteristic rscFeature     = BLECharacteristic(0x2A54);
BLECharacteristic rscSensorLoc   = BLECharacteristic(0x2A5D);

uint8_t customServiceUuid[16] = {
  0xFB, 0x34, 0x9B, 0x5F, 0x80, 0x00, 0x00, 0x80,
  0x00, 0x10, 0x00, 0x00, 0x00, 0xFE, 0x00, 0x00
};
uint8_t inclinCharUuid[16] = {
  0xFB, 0x34, 0x9B, 0x5F, 0x80, 0x00, 0x00, 0x80,
  0x00, 0x10, 0x00, 0x00, 0x01, 0xFE, 0x00, 0x00
};
BLEService customService = BLEService(customServiceUuid);
BLECharacteristic rscInclinaison = BLECharacteristic(inclinCharUuid);

// ---- VITESSE ----
float wheelCircM = (48.0 * PI) / 1000.0;   // recomputed on SET wheel_diam_mm
volatile unsigned long lastPulseTime = 0;
volatile unsigned long pulseInterval = 0;
volatile bool newPulse = false;
volatile uint32_t pulseCount = 0;
uint32_t pulseCountBase = 0;   // pulseCount snapshot at last RESET_DIST

float speedKmhRaw = 0.0;
float speedMsRaw = 0.0;
float smoothSpeedMs = 0.0;
float distanceM = 0.0;
bool treadmillRunning = false;

// ---- INCLINAISON (VL53L4CD via Pololu lib) ----
// `tofRawMm` raw sample, `tofSmoothMm` median+EMA smoothed (both millimetres —
// serial carries mm; the Pi does the mm->% mapping). `tofPercent` is the
// firmware-side 2-point calibrated percent, used ONLY as the BLE fallback when
// the Pi's BLE_INCLIN push goes stale. tofCal0/12 are that fallback's points.
bool tofOk = false;
unsigned long lastTofGood = 0;
uint16_t tofRawMm = 0;
float tofSmoothMm = 0.0;
bool tofSmoothSeeded = false;
float tofPercent = 0.0;
uint8_t tofStatus = 255;
int16_t tofCal0Mm = -1;           // -1 sentinel = not set
int16_t tofCal12Mm = -1;

// Incline pushed from the Pi (already fully 6-point calibrated + offset).
float piInclinPct = 0.0;
unsigned long piInclinLastMs = 0;  // 0 = never received

// Cadence pushed from the Pi (its band-pass + windowed-autocorrelation detector,
// far more robust than a frame-mounted threshold counter). Wins over the local
// detector while fresh; falls back to it (then to the synthetic estimate) if the
// push goes stale — symmetric to piInclin above.
float piCadenceSpm = 0.0;
unsigned long piCadenceLastMs = 0;  // 0 = never received

// ---- CADENCE (real, measured from LSM303 footfall impacts) ----
bool imuOk = false;
float accelBaseline = 9.81;
float accelDynamic = 0.0;
float accelDynPeak = 0.0;
bool  stepArmed = true;
unsigned long lastStepTime = 0;
float stepCadenceSpm = 0.0;
uint32_t stepCount = 0;
unsigned long lastAccelRead = 0;

// ---- RAW ACCEL RECORDING (offline cadence calibration) ----
bool rawStreamOn = false;
unsigned long rawStreamStart = 0;

// Why we last (re)booted (nRF RESETREAS; bit 1 = watchdog). Also reported in
// STATUS, which the Pi asks for on every connect — the boot banner itself is
// usually gone before the Pi has reopened the port.
uint32_t resetReason = 0;

// ---- BLE STATE ----
bool bleActive = true;

// ---- TIMING ----
unsigned long lastTofRead = 0;
unsigned long lastBle = 0;
unsigned long lastSerial = 0;
unsigned long loopCount = 0;
unsigned long maxLoopTime = 0;

// ==============================
// ISR
// ==============================
void hallISR() {
  unsigned long now = millis();
  unsigned long diff = now - lastPulseTime;
  if (diff > debounceMs) {
    pulseInterval = diff;
    lastPulseTime = now;
    newPulse = true;
    pulseCount++;
  }
}

// ==============================
// Speed
// ==============================
void updateSpeed() {
  // millis() is read INSIDE the critical section: read before it, a pulse
  // landing in between made (now - lastPulse) underflow and zeroed the speed.
  noInterrupts();
  unsigned long now = millis();
  unsigned long lastPulse = lastPulseTime;
  unsigned long interval = pulseInterval;
  bool gotPulse = newPulse;
  uint32_t snapCount = pulseCount;
  newPulse = false;
  interrupts();

  unsigned long elapsed = now - lastPulse;

  if (gotPulse && interval > 0) {
    speedMsRaw = wheelCircM / (interval / 1000.0);
    speedKmhRaw = speedMsRaw * 3.6;
    treadmillRunning = true;

    if (smoothSpeedMs < 0.01) {
      smoothSpeedMs = speedMsRaw;
    } else {
      smoothSpeedMs = speedEma * speedMsRaw + (1.0f - speedEma) * smoothSpeedMs;
    }
  }

  // Distance from the exact ISR pulse count (not a per-flag accumulator): the
  // main loop can block on BLE notify, during which the ISR still counts every
  // pulse but only the last was flagged — an accumulator silently loses metres.
  distanceM = (float)(snapCount - pulseCountBase) * wheelCircM;

  if (elapsed > speedTimeoutMs) {
    speedKmhRaw = 0.0;
    speedMsRaw = 0.0;
    smoothSpeedMs = 0.0;
    treadmillRunning = false;
  }
}

// Speed-derived estimate. Safe fallback in broadcastCadenceSpm() when the IMU
// footfall detector isn't delivering.
uint8_t computeCadence(float speedMs) {
  if (speedMs < 0.05) return 0;
  float strideLen = 0.5 + (speedMs * 0.15);
  if (strideLen < 0.4) strideLen = 0.4;
  if (strideLen > 2.0) strideLen = 2.0;
  float cad = (speedMs / strideLen) * 60.0;
  if (cad < 40) cad = 40;
  if (cad > 220) cad = 220;
  return (uint8_t)cad;
}

// ==============================
// Cadence — real footfall detection (LSM303)
// ==============================
void updateCadence() {
  if (!imuOk) return;

  sensors_event_t ev;
  accel.getEvent(&ev);
  float mag = sqrtf(ev.acceleration.x * ev.acceleration.x +
                    ev.acceleration.y * ev.acceleration.y +
                    ev.acceleration.z * ev.acceleration.z);

  // Raw recording: emit the untouched sample (integers, mm/s²) if streaming.
  if (rawStreamOn) {
    if (millis() - rawStreamStart > 3600000UL) {
      rawStreamOn = false;   // 60 min auto-off safety
    } else {
      char buf[52];
      int n = snprintf(buf, sizeof(buf), "$RAW,%lu,%d,%d,%d,%d\n",
                       millis(),
                       (int)(ev.acceleration.x * 1000.0f),
                       (int)(ev.acceleration.y * 1000.0f),
                       (int)(ev.acceleration.z * 1000.0f),
                       (int)(smoothSpeedMs * 1000.0f));
      // Only while the queue is under half full: when the host reads slowly
      // the 100 Hz stream must not crowd out $DATA and config acks.
      if (n > 0 && n < (int)sizeof(buf) && Out.freeBytes() > TXQ_SIZE / 2) {
        Out.write((const uint8_t*)buf, n);   // dropped whole if the queue is full
      }
    }
  }

  // Slow baseline tracks gravity + motor hum but is too slow to follow a
  // per-step transient, so the strike survives in `dynamic`.
  accelBaseline = (1.0f - accelBaseEma) * accelBaseline + accelBaseEma * mag;
  accelDynamic = mag - accelBaseline;
  if (accelDynamic > accelDynPeak) accelDynPeak = accelDynamic;  // peak-hold

  unsigned long now = millis();

  // Rising-edge detect with hysteresis + refractory window.
  if (stepArmed && accelDynamic > cadThreshold &&
      (now - lastStepTime) > cadRefractMs) {
    if (lastStepTime > 0) {
      float instSpm = 60000.0f / (float)(now - lastStepTime);
      if (instSpm > 250.0f) instSpm = 250.0f;
      if (stepCadenceSpm < 1.0f) stepCadenceSpm = instSpm;
      else stepCadenceSpm = 0.3f * instSpm + 0.7f * stepCadenceSpm;
    }
    lastStepTime = now;
    stepCount++;
    stepArmed = false;
  }

  // Re-arm once the signal settles (avoids counting one strike's ring as many).
  if (!stepArmed && accelDynamic < cadRearm) {
    stepArmed = true;
  }

  // No strike for a while → runner stopped (or detection lost): decay to 0.
  if (now - lastStepTime > cadTimeoutMs) {
    stepCadenceSpm = 0.0f;
  }
}

// Pure measured cadence from footfall detection (steps/min), 0 when no recent
// strike. The honest signal used for tuning telemetry.
uint8_t realCadenceSpm() {
  float spm = stepCadenceSpm;
  if (spm < 0.0f) spm = 0.0f;
  if (spm > 250.0f) spm = 250.0f;
  return (uint8_t)(spm + 0.5f);
}

// Cadence we broadcast (BLE + serial). Prefer the real measured value; fall
// back to the speed-derived estimate when the IMU is absent or hasn't seen a
// footfall recently, so Garmin never sits at 0.
uint8_t broadcastCadenceSpm() {
  // Pi's calibrated detector wins whenever its push is fresh.
  if (piCadenceLastMs != 0 && (millis() - piCadenceLastMs) < PI_CADENCE_TIMEOUT) {
    float spm = piCadenceSpm;
    if (spm < 0.0f) spm = 0.0f;
    if (spm > 250.0f) spm = 250.0f;
    return (uint8_t)(spm + 0.5f);
  }
  bool imuLive = imuOk && stepCadenceSpm > 1.0f &&
                 (millis() - lastStepTime) < cadTimeoutMs;
  if (imuLive) return realCadenceSpm();
  return computeCadence(smoothSpeedMs * paceFactor);   // synthetic fallback
}

// ==============================
// VL53L4CD time-of-flight (Pololu API)
// ==============================
// Fallback-only mm → percent mapping (two captured points). Used for the BLE
// incline characteristic ONLY when the Pi's BLE_INCLIN push is stale. Returns
// 0.0 when calibration isn't set yet.
float mmToPercent(float mm) {
  if (tofCal0Mm < 0 || tofCal12Mm < 0) return 0.0;
  if (tofCal12Mm == tofCal0Mm) return 0.0;   // degenerate, avoid div/0
  float slope = (float)TOF_CAL_TARGET_HI / (float)(tofCal12Mm - tofCal0Mm);
  float pct = slope * (mm - (float)tofCal0Mm);
  if (pct < -2.0f) pct = -2.0f;
  if (pct > (float)TOF_CAL_TARGET_HI + 2.0f) pct = TOF_CAL_TARGET_HI + 2.0f;
  return pct;
}

void readTOF() {
  if (!tofOk) return;
  if (!sensor.dataReady()) return;
  sensor.read(false);

  if (sensor.timeoutOccurred()) {
    tofStatus = 254;     // distinct from the lib's 0-13 status codes
    return;
  }

  tofStatus = sensor.ranging_data.range_status;
  if (tofStatus != 0) return;

  tofRawMm = sensor.ranging_data.range_mm;
  lastTofGood = millis();

  // Median over the most recent tofMedN samples (100 ms each).
  static uint16_t medBuf[21] = {0};
  static uint8_t medIdx = 0;
  static uint8_t medCount = 0;
  medBuf[medIdx] = tofRawMm;
  medIdx = (medIdx + 1) % 21;
  if (medCount < 21) medCount++;

  uint16_t sorted[21];
  int n = tofMedN;
  if (n > medCount) n = medCount;
  for (int i = 0; i < n; i++) sorted[i] = medBuf[(medIdx + 21 - 1 - i) % 21];
  for (int i = 0; i < n - 1; i++)
    for (int j = i + 1; j < n; j++)
      if (sorted[j] < sorted[i]) { uint16_t t = sorted[i]; sorted[i] = sorted[j]; sorted[j] = t; }
  float median = (float)sorted[n / 2];

  if (!tofSmoothSeeded) {
    tofSmoothMm = median;
    tofSmoothSeeded = true;
  } else {
    // Slow while steady, fast once a sustained one-sided deviation shows the
    // deck is really moving (see the tofMedN comment).
    static int8_t devRun = 0;
    float dev = median - tofSmoothMm;
    if (dev > tofFastMm)       devRun = (devRun > 0) ? (devRun < 100 ? devRun + 1 : 100) : 1;
    else if (dev < -tofFastMm) devRun = (devRun < 0) ? (devRun > -100 ? devRun - 1 : -100) : -1;
    else                       devRun = 0;
    bool moving = (devRun >= TOF_FAST_CONFIRM) || (devRun <= -TOF_FAST_CONFIRM);
    float a = moving ? tofEmaFast : tofEma;
    tofSmoothMm = a * median + (1.0f - a) * tofSmoothMm;
  }

  tofPercent = mmToPercent(tofSmoothMm);
}

// ==============================
// BLE
// ==============================
void setupBLE() {
  Bluefruit.begin();
  Bluefruit.setTxPower(4);
  Bluefruit.setName("Treadmill FP");
  Bluefruit.setAppearance(0x0441);
  Bluefruit.Periph.setConnectCallback(bleConnectCallback);
  Bluefruit.Periph.setDisconnectCallback(bleDisconnectCallback);

  rscService.begin();

  rscSensorLoc.setProperties(CHR_PROPS_READ);
  rscSensorLoc.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  rscSensorLoc.setFixedLen(1);
  rscSensorLoc.begin();
  uint8_t loc = 1;
  rscSensorLoc.write(&loc, 1);

  rscFeature.setProperties(CHR_PROPS_READ);
  rscFeature.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  rscFeature.setFixedLen(2);
  rscFeature.begin();
  uint16_t feat = 0x0000;
  rscFeature.write((uint8_t*)&feat, 2);

  rscMeasurement.setProperties(CHR_PROPS_NOTIFY);
  rscMeasurement.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  rscMeasurement.setFixedLen(4);
  rscMeasurement.begin();

  customService.begin();
  rscInclinaison.setProperties(CHR_PROPS_NOTIFY | CHR_PROPS_READ);
  rscInclinaison.setPermission(SECMODE_OPEN, SECMODE_NO_ACCESS);
  rscInclinaison.setFixedLen(2);
  rscInclinaison.begin();

  Bluefruit.Advertising.restartOnDisconnect(false);
  startAdvertising();
}

void startAdvertising() {
  Bluefruit.Advertising.clearData();
  Bluefruit.Advertising.addFlags(BLE_GAP_ADV_FLAGS_LE_ONLY_GENERAL_DISC_MODE);
  Bluefruit.Advertising.addTxPower();
  Bluefruit.Advertising.addService(rscService);
  Bluefruit.Advertising.addAppearance(0x0441);
  Bluefruit.Advertising.addName();
  Bluefruit.Advertising.setInterval(160, 244);
  Bluefruit.Advertising.setFastTimeout(30);
  Bluefruit.Advertising.start(0);
}

// These run in the Bluefruit callback task: only raise a flag, loop() prints it
// (the serial queue is single-task).
volatile bool bleEvtConnected = false;
volatile bool bleEvtDisconnected = false;
volatile bool bleAdvRestart = false;

void bleConnectCallback(uint16_t conn_handle) {
  bleEvtConnected = true;
}

void bleDisconnectCallback(uint16_t conn_handle, uint8_t reason) {
  bleEvtDisconnected = true;
  bleAdvRestart = true;   // done from loop(): it also drives Advertising
}

void reportBleEvents() {
  if (bleAdvRestart) {
    bleAdvRestart = false;
    if (bleActive && !Bluefruit.connected()) startAdvertising();
  }
  if (bleEvtConnected) {
    bleEvtConnected = false;
    Out.println("[BLE_EVENT] CONNECTED");
  }
  if (bleEvtDisconnected) {
    bleEvtDisconnected = false;
    Out.println("[BLE_EVENT] DISCONNECTED");
  }
}

void sendRscData() {
  if (!Bluefruit.connected() || !bleActive) return;

  float corrSpeedMs = smoothSpeedMs * paceFactor;
  uint8_t cadence = broadcastCadenceSpm();   // measured, or synthetic fallback
  uint16_t bleSpeed = (uint16_t)(corrSpeedMs * 256.0);

  uint8_t data[4];
  data[0] = 0x00;
  data[1] = bleSpeed & 0xFF;
  data[2] = (bleSpeed >> 8) & 0xFF;
  // RSC "Instantaneous Cadence" is per-foot (strides/min); Garmin DOUBLES this
  // byte for its steps/min display, so send half of the full steps/min.
  data[3] = (uint8_t)((cadence + 1) / 2);

  if (!rscMeasurement.notify(data, 4)) {
    delay(3);
    rscMeasurement.notify(data, 4);
  }

  // Incline: prefer the Pi's 6-point calibrated push (already offset-applied);
  // fall back to the firmware's own 2-point mapping if the push is stale, so a
  // Pi-less Feather->Garmin still shows a sane grade.
  float corrInclin;
  if (piInclinLastMs != 0 && (millis() - piInclinLastMs) < PI_INCLIN_TIMEOUT) {
    corrInclin = piInclinPct;
  } else {
    corrInclin = tofPercent + inclinOffset;
  }
  corrInclin = clampf(corrInclin, -50.0f, 50.0f);
  int16_t bleInclin = (int16_t)(corrInclin * 100.0f);
  rscInclinaison.notify((uint8_t*)&bleInclin, 2);
}

void enableBLE() {
  if (!bleActive) {
    bleActive = true;
    startAdvertising();
    Out.println("[BLE_EVENT] BLE_ON");
  }
}

void disableBLE() {
  if (bleActive) {
    bleActive = false;
    if (Bluefruit.connected()) {
      Bluefruit.disconnect(Bluefruit.connHandle());
    }
    Bluefruit.Advertising.stop();
    Out.println("[BLE_EVENT] BLE_OFF");
  }
}

// ==============================
// Runtime config parameter table
// ==============================
// One place defines every SET-able key. getParam() reads a key back (for the
// ack echo, GET and CFG_DUMP); applyParam() validates+assigns. Keep the two in
// sync. Values are printed to the Pi as floats — it parses numerically.
const char* PARAM_KEYS[] = {
  "pace_factor", "inclin_offset", "cad_threshold", "cad_rearm",
  "cad_refract_ms", "cad_timeout_ms", "wheel_diam_mm", "debounce_ms",
  "speed_timeout_ms", "ble_update_ms", "serial_update_ms",
  "speed_ema", "tof_ema", "accel_base_ema", "tof_cal_0_mm", "tof_cal_12_mm",
  "tof_med_n", "tof_ema_fast", "tof_fast_mm"
};
const int PARAM_COUNT = sizeof(PARAM_KEYS) / sizeof(PARAM_KEYS[0]);

float getParam(const String& k) {
  if (k == "pace_factor")      return paceFactor;
  if (k == "inclin_offset")    return inclinOffset;
  if (k == "cad_threshold")    return cadThreshold;
  if (k == "cad_rearm")        return cadRearm;
  if (k == "cad_refract_ms")   return cadRefractMs;
  if (k == "cad_timeout_ms")   return cadTimeoutMs;
  if (k == "wheel_diam_mm")    return wheelDiamMm;
  if (k == "debounce_ms")      return debounceMs;
  if (k == "speed_timeout_ms") return speedTimeoutMs;
  if (k == "ble_update_ms")    return bleUpdateMs;
  if (k == "serial_update_ms") return serialUpdateMs;
  if (k == "speed_ema")        return speedEma;
  if (k == "tof_ema")          return tofEma;
  if (k == "accel_base_ema")   return accelBaseEma;
  if (k == "tof_cal_0_mm")     return tofCal0Mm;
  if (k == "tof_cal_12_mm")    return tofCal12Mm;
  if (k == "tof_med_n")        return tofMedN;
  if (k == "tof_ema_fast")     return tofEmaFast;
  if (k == "tof_fast_mm")      return tofFastMm;
  return 0.0f;   // unknown key (callers gate on isKnownParam first)
}

// Returns true if the key is known and the value accepted.
bool applyParam(const String& k, float v) {
  if (k == "pace_factor")           paceFactor = clampf(v, 0.1f, 5.0f);
  else if (k == "inclin_offset")    inclinOffset = v;
  else if (k == "cad_threshold")    cadThreshold = v;
  else if (k == "cad_rearm")        cadRearm = v;
  else if (k == "cad_refract_ms")   cadRefractMs = (uint16_t)v;
  else if (k == "cad_timeout_ms")   cadTimeoutMs = (uint16_t)v;
  else if (k == "wheel_diam_mm") {
    wheelDiamMm = clampf(v, 5.0f, 500.0f);
    wheelCircM = (wheelDiamMm * PI) / 1000.0;   // keep derived value in sync
  }
  else if (k == "debounce_ms")      debounceMs = (uint16_t)v;
  else if (k == "speed_timeout_ms") speedTimeoutMs = (uint16_t)v;
  else if (k == "ble_update_ms")    bleUpdateMs = (uint16_t)clampf(v, 50.0f, 5000.0f);
  else if (k == "serial_update_ms") serialUpdateMs = (uint16_t)clampf(v, 50.0f, 5000.0f);
  else if (k == "speed_ema")        speedEma = clampf(v, 0.01f, 1.0f);
  else if (k == "tof_ema")          tofEma = clampf(v, 0.01f, 1.0f);
  else if (k == "accel_base_ema")   accelBaseEma = clampf(v, 0.001f, 1.0f);
  else if (k == "tof_cal_0_mm") {
    if (v < TOF_CAL_MIN_MM || v > TOF_CAL_MAX_MM) return false;
    tofCal0Mm = (int16_t)v;
  }
  else if (k == "tof_cal_12_mm") {
    if (v < TOF_CAL_MIN_MM || v > TOF_CAL_MAX_MM) return false;
    tofCal12Mm = (int16_t)v;
  }
  else if (k == "tof_med_n") {
    uint8_t n = (uint8_t)clampf(v, 1.0f, 21.0f);
    tofMedN = n | 1;                              // keep it odd
  }
  else if (k == "tof_ema_fast")     tofEmaFast = clampf(v, 0.01f, 1.0f);
  else if (k == "tof_fast_mm")      tofFastMm = clampf(v, 0.5f, 500.0f);
  else return false;
  return true;
}

bool isKnownParam(const String& k) {
  for (int i = 0; i < PARAM_COUNT; i++) {
    if (k == PARAM_KEYS[i]) return true;
  }
  return false;
}

void printParam(const String& k) {
  Out.print("[CONFIG] "); Out.print(k); Out.print("=");
  Out.println(getParam(k), 3);
}

// ==============================
// Serial Commands (non-blocking line assembler)
// ==============================
void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  // ---- generic runtime config ----
  if (cmd.startsWith("SET ")) {
    int sp = cmd.indexOf(' ', 4);
    if (sp < 0) { Out.println("[CONFIG] bad SET (need: SET key value)"); return; }
    String key = cmd.substring(4, sp);
    String val = cmd.substring(sp + 1);
    key.trim(); val.trim();
    if (applyParam(key, val.toFloat())) printParam(key);
    else { Out.print("[CONFIG] rejected "); Out.println(key); }
  }
  else if (cmd.startsWith("GET ")) {
    String key = cmd.substring(4); key.trim();
    if (isKnownParam(key)) printParam(key);
    else { Out.print("[CONFIG] unknown "); Out.println(key); }
  }
  else if (cmd == "CFG_DUMP") {
    for (int i = 0; i < PARAM_COUNT; i++) printParam(String(PARAM_KEYS[i]));
    Out.println("[CONFIG] END");
  }
  // ---- incline pushed from the Pi (high rate, no echo) ----
  else if (cmd.startsWith("BLE_INCLIN:")) {
    piInclinPct = cmd.substring(11).toFloat();
    piInclinLastMs = millis();
  }
  // ---- cadence pushed from the Pi (high rate, no echo) ----
  else if (cmd.startsWith("BLE_CAD:")) {
    piCadenceSpm = cmd.substring(8).toFloat();
    piCadenceLastMs = millis();
  }
  // ---- actions ----
  else if (cmd == "BLE_ON") enableBLE();
  else if (cmd == "BLE_OFF") disableBLE();
  else if (cmd == "RESET_DIST") {
    noInterrupts();
    pulseCountBase = pulseCount;
    interrupts();
    distanceM = 0.0;
    Out.println("[CONFIG] distance reset");
  }
  else if (cmd == "TOF_CAL_CLEAR") {
    tofCal0Mm = -1;
    tofCal12Mm = -1;
    Out.println("[CONFIG] tof_cal cleared");
  }
  else if (cmd.startsWith("CAD_RAW:")) {
    rawStreamOn = (cmd.substring(8).toInt() != 0);
    if (rawStreamOn) rawStreamStart = millis();
    Out.print("[CONFIG] cad_raw="); Out.println(rawStreamOn ? 1 : 0);
  }
  // ---- dumps / diagnostics ----
  else if (cmd == "STATUS") {
    Out.print("[STATUS] FW:"); Out.print(FW_VERSION);
    Out.print(",BLE:"); Out.print(bleActive ? "ON" : "OFF");
    Out.print(",CONN:"); Out.print(Bluefruit.connected() ? "YES" : "NO");
    Out.print(",PF:"); Out.print(paceFactor, 3);
    Out.print(",IO:"); Out.print(inclinOffset, 2);
    Out.print(",TOF:"); Out.print(tofOk ? "OK" : "FAIL");
    Out.print(",CAL0:"); Out.print(tofCal0Mm);
    Out.print(",CAL12:"); Out.print(tofCal12Mm);
    Out.print(",IMU:"); Out.print(imuOk ? "OK" : "FAIL");
    bool piCadFresh = piCadenceLastMs != 0 &&
                      (millis() - piCadenceLastMs) < PI_CADENCE_TIMEOUT;
    Out.print(",CADSRC:"); Out.print(piCadFresh ? "PI" : "LOCAL");
    Out.print(",CAD:"); Out.print(broadcastCadenceSpm());
    Out.print(",LOOP_MAX:"); Out.print(maxLoopTime);
    Out.print("ms,TXDROP:"); Out.print(Out.dropped());
    Out.print(",RST:0x"); Out.println(resetReason, HEX);
  }
  else if (cmd == "DIAG") {
    Out.print("[DIAG] max_loop="); Out.print(maxLoopTime);
    Out.print("ms, loops="); Out.print(loopCount);
    Out.print(", tx_dropped="); Out.println(Out.dropped());
    maxLoopTime = 0;
    loopCount = 0;
  }
  else if (cmd == "TOF") {
    Out.print("[TOF] raw_mm="); Out.print(tofRawMm);
    Out.print(" smooth_mm="); Out.print(tofSmoothMm, 1);
    Out.print(" status="); Out.print(tofStatus);
    Out.print(" cal0="); Out.print(tofCal0Mm);
    Out.print(" cal12="); Out.print(tofCal12Mm);
    Out.print(" percent="); Out.println(tofPercent, 2);
  }
  else if (cmd == "CAD") {
    Out.print("[CAD] dyn="); Out.print(accelDynamic, 2);
    Out.print(" base="); Out.print(accelBaseline, 2);
    Out.print(" thr="); Out.print(cadThreshold, 2);
    Out.print(" rearm="); Out.print(cadRearm, 2);
    Out.print(" refract="); Out.print(cadRefractMs);
    Out.print(" spm="); Out.print(stepCadenceSpm, 0);
    Out.print(" steps="); Out.print(stepCount);
    Out.print(" imu="); Out.println(imuOk ? "OK" : "FAIL");
  }
  else {
    Out.print("[CONFIG] unknown command: "); Out.println(cmd);
  }
}

// Non-blocking: drain the RX buffer, dispatch a command per newline. Never
// stalls the loop (unlike Serial.readStringUntil, which blocked up to 1 s on a
// partial line and glitched the 100 Hz cadence loop during the config burst).
void processSerialInput() {
  static char buf[96];
  static uint8_t len = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (len > 0) {
        buf[len] = '\0';
        handleCommand(String(buf));
        len = 0;
      }
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    } else {
      len = 0;   // overflow: drop the runaway line rather than truncate-dispatch
    }
  }
}

// ==============================
// Serial Data to Pi
// ==============================
void sendSerialData() {
  float corrSpeedMs = smoothSpeedMs * paceFactor;
  uint8_t cadence = broadcastCadenceSpm();

  // ToF healthy only if it produced a valid sample recently (tofOk alone is set
  // once in setup() and never cleared).
  bool tofFresh = tofOk && (millis() - lastTofGood < 5000);

  Out.print("$DATA,");
  Out.print(speedKmhRaw, 2);     Out.print(",");
  Out.print(smoothSpeedMs, 4);   Out.print(",");
  Out.print(cadence);            Out.print(",");
  Out.print(distanceM, 2);       Out.print(",");
  Out.print(tofSmoothMm, 1);     Out.print(",");
  Out.print(pulseCount - pulseCountBase); Out.print(",");
  Out.print(bleActive ? 1 : 0);  Out.print(",");
  Out.print(Bluefruit.connected() ? 1 : 0); Out.print(",");
  Out.print(tofRawMm);           Out.print(",");
  Out.print(tofFresh ? 1 : 0);   Out.print(",");
  // Cadence-tuning telemetry: [11]=impact peak, [12]=step count, [13]=IMU
  // health, [14]=real measured cadence (before fallback).
  Out.print(accelDynPeak, 2);    Out.print(",");
  Out.print(stepCount);          Out.print(",");
  Out.print(imuOk ? 1 : 0);      Out.print(",");
  Out.println(realCadenceSpm());
  accelDynPeak = 0.0;   // reset the peak-hold window
}

// ==============================
// SETUP
// ==============================
void setup() {
  // Why did we (re)boot? Captured by the core at startup. Bit 1 = watchdog.
  resetReason = readResetReason();

  // Armed before the sensor inits so a wedged I2C bus at boot also recovers.
  wdtStart();

  Serial.begin(115200);
  delay(2000);
  wdtFeed();

  Out.print("$HELLO,"); Out.println(FW_VERSION);
  Out.print("[BOOT] Treadmill Sensor "); Out.print(FW_VERSION);
  Out.println(" (VL53L4CD Pololu)");
  Out.print("[BOOT] reset_reason=0x"); Out.print(resetReason, HEX);
  Out.println((resetReason & POWER_RESETREAS_DOG_Msk) ? " WATCHDOG" : "");

  pinMode(HALL_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(HALL_PIN), hallISR, FALLING);
  Out.println("[OK] DRV5023");

  Wire.begin();
  Wire.setClock(400000);   // 400 kHz I2C

  // LSM303
  Wire.beginTransmission(0x19);
  uint8_t i2cErr = Wire.endTransmission();
  if (i2cErr != 0) {
    Out.print("[WARN] LSM303 not on I2C (err=");
    Out.print(i2cErr);
    Out.println(") - wiring may have changed");
  } else if (!accel.begin()) {
    Out.println("[WARN] LSM303 begin failed");
  } else {
    accel.setRange(LSM303_RANGE_2G);
    accel.setMode(LSM303_MODE_NORMAL);
    imuOk = true;
    Out.println("[OK] LSM303");
  }

  // VL53L4CD time-of-flight — Pololu API.
  sensor.setTimeout(TOF_I2C_TIMEOUT);
  if (!sensor.init()) {
    Out.println("[ERR] VL53L4CD init failed (no I2C ack at 0x29)");
    tofOk = false;
  } else {
    sensor.setRangeTiming(TOF_TIMING_BUDGET, TOF_INTER_MEAS);
    sensor.startContinuous();
    tofOk = true;
    Out.println("[OK] VL53L4CD");
  }

  setupBLE();
  Out.println("[OK] BLE");

  // The Pi waits for this line before pushing config, then verifies each SET.
  Out.println("[BOOT] READY");
  wdtFeed();
}

// ==============================
// LOOP
// ==============================
void loop() {
  unsigned long loopStart = millis();
  unsigned long now = loopStart;

  wdtFeed();
  reportBleEvents();
  processSerialInput();
  updateSpeed();

  if (now - lastAccelRead >= ACCEL_READ_MS) {
    lastAccelRead = now;
    updateCadence();
  }

  if (now - lastTofRead >= TOF_READ_MS) {
    lastTofRead = now;
    readTOF();
  }

  if (bleActive && (now - lastBle >= bleUpdateMs)) {
    lastBle = now;
    sendRscData();
  }

  if (now - lastSerial >= serialUpdateMs) {
    lastSerial = now;
    sendSerialData();
  }

  Out.pump();   // drain queued lines to USB, as much as fits, never blocking

  unsigned long loopTime = millis() - loopStart;
  if (loopTime > maxLoopTime) maxLoopTime = loopTime;
  loopCount++;
}
