#include <bluefruit.h>
#include <Wire.h>
#include <Adafruit_LSM303_Accel.h>
#include <Adafruit_Sensor.h>

// ---- CONFIG ----
#define HALL_PIN        A0
#define WHEEL_DIAM_MM  55.0
#define DEBOUNCE_MS    15
#define BLE_UPDATE_MS  500
#define SERIAL_UPDATE_MS 500
#define SPEED_TIMEOUT_MS 3000
#define IMU_READ_MS    300

// ---- OBJETS ----
Adafruit_LSM303_Accel_Unified accel = Adafruit_LSM303_Accel_Unified(54321);

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
const float wheelCircM = (WHEEL_DIAM_MM * PI) / 1000.0;
volatile unsigned long lastPulseTime = 0;
volatile unsigned long pulseInterval = 0;
volatile bool newPulse = false;
volatile uint32_t pulseCount = 0;

float speedKmhRaw = 0.0;
float speedMsRaw = 0.0;
float smoothSpeedMs = 0.0;
float distanceM = 0.0;
bool treadmillRunning = false;

float paceFactor = 1.0;
float inclinOffset = 0.0;

// ---- INCLINAISON ----
float inclinaisonRaw = 0.0;
float inclinaisonY = 0.0;    // Lissee
bool imuOk = true;
uint8_t imuFailCount = 0;
float inclinSmooth = 0.0;
float inclinZero = 0.0;       // Offset de mise a zero
int8_t inclinSign = 1;        // 1 ou -1 pour inverser

// ---- BLE STATE ----
bool bleActive = true;

// ---- TIMING ----
unsigned long lastImuRead = 0;
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
  if (diff > DEBOUNCE_MS) {
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
  unsigned long now = millis();

  noInterrupts();
  unsigned long lastPulse = lastPulseTime;
  unsigned long interval = pulseInterval;
  bool gotPulse = newPulse;
  newPulse = false;
  interrupts();

  unsigned long elapsed = now - lastPulse;

  if (gotPulse && interval > 0) {
    speedMsRaw = wheelCircM / (interval / 1000.0);
    speedKmhRaw = speedMsRaw * 3.6;
    distanceM += wheelCircM;
    treadmillRunning = true;

    if (smoothSpeedMs < 0.01) {
      smoothSpeedMs = speedMsRaw;
    } else {
      smoothSpeedMs = 0.4 * speedMsRaw + 0.6 * smoothSpeedMs;
    }
  }

  if (elapsed > SPEED_TIMEOUT_MS) {
    speedKmhRaw = 0.0;
    speedMsRaw = 0.0;
    smoothSpeedMs = 0.0;
    treadmillRunning = false;
  }
}

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
// IMU - LSM303 accelerometre
// ==============================
void readIMU() {
  sensors_event_t event;
  if (!accel.getEvent(&event)) {
    imuFailCount++;
    if (imuFailCount > 20) {
      Wire.end();
      delay(2);
      Wire.begin();
      imuFailCount = 0;
      Serial.println("[WARN] I2C reset");
    }
    return;
  }

  imuFailCount = 0;

  float ax = event.acceleration.x;
  float ay = event.acceleration.y;
  float az = event.acceleration.z;

  float magnitude = sqrt(ax * ax + ay * ay + az * az);
  if (magnitude > 0.1) {
    inclinaisonRaw = atan2(ay, sqrt(ax * ax + az * az)) * 180.0 / PI;
  }

  // EMA lissage (alpha=0.15 pour stabilite, reduit le bruit de +/-0.3)
  inclinSmooth = 0.15 * inclinaisonRaw + 0.85 * inclinSmooth;

  // Appliquer zero + inversion
  inclinaisonY = (inclinSmooth - inclinZero) * inclinSign;
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

void bleConnectCallback(uint16_t conn_handle) {
  Serial.println("[BLE_EVENT] CONNECTED");
}

void bleDisconnectCallback(uint16_t conn_handle, uint8_t reason) {
  Serial.println("[BLE_EVENT] DISCONNECTED");
  if (bleActive) startAdvertising();
}

void sendRscData() {
  if (!Bluefruit.connected() || !bleActive) return;

  float corrSpeedMs = smoothSpeedMs * paceFactor;
  uint8_t cadence = computeCadence(corrSpeedMs);
  uint16_t bleSpeed = (uint16_t)(corrSpeedMs * 256.0);

  uint8_t data[4];
  data[0] = 0x00;
  data[1] = bleSpeed & 0xFF;
  data[2] = (bleSpeed >> 8) & 0xFF;
  data[3] = cadence;

  if (!rscMeasurement.notify(data, 4)) {
    delay(3);
    rscMeasurement.notify(data, 4);
  }

  float corrInclin = inclinaisonY + inclinOffset;
  int16_t bleInclin = (int16_t)(corrInclin * 100.0);
  rscInclinaison.notify((uint8_t*)&bleInclin, 2);
}

void enableBLE() {
  if (!bleActive) {
    bleActive = true;
    startAdvertising();
    Serial.println("[BLE_EVENT] BLE_ON");
  }
}

void disableBLE() {
  if (bleActive) {
    bleActive = false;
    if (Bluefruit.connected()) {
      Bluefruit.disconnect(Bluefruit.connHandle());
    }
    Bluefruit.Advertising.stop();
    Serial.println("[BLE_EVENT] BLE_OFF");
  }
}

// ==============================
// Serial Commands
// ==============================
void processSerialCommand() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();

    if (cmd == "BLE_ON") enableBLE();
    else if (cmd == "BLE_OFF") disableBLE();
    else if (cmd.startsWith("PACE_FACTOR:")) {
      paceFactor = cmd.substring(12).toFloat();
      if (paceFactor < 0.1) paceFactor = 0.1;
      if (paceFactor > 5.0) paceFactor = 5.0;
      Serial.print("[CONFIG] pace_factor="); Serial.println(paceFactor, 3);
    }
    else if (cmd.startsWith("INCLIN_OFFSET:")) {
      inclinOffset = cmd.substring(14).toFloat();
      Serial.print("[CONFIG] inclin_offset="); Serial.println(inclinOffset, 2);
    }
    else if (cmd == "RESET_DIST") {
      distanceM = 0.0;
      pulseCount = 0;
      Serial.println("[CONFIG] distance reset");
    }
    else if (cmd == "INCLIN_ZERO") {
      inclinZero = inclinSmooth;
      Serial.print("[CONFIG] inclin_zero="); Serial.println(inclinZero, 2);
    }
    else if (cmd == "INCLIN_INVERT") {
      inclinSign = -inclinSign;
      Serial.print("[CONFIG] inclin_sign="); Serial.println(inclinSign);
    }
    else if (cmd.startsWith("INCLIN_SIGN:")) {
      int s = cmd.substring(12).toInt();
      inclinSign = (s < 0) ? -1 : 1;
      Serial.print("[CONFIG] inclin_sign="); Serial.println(inclinSign);
    }
    else if (cmd.startsWith("INCLIN_ZERO_VAL:")) {
      inclinZero = cmd.substring(16).toFloat();
      Serial.print("[CONFIG] inclin_zero="); Serial.println(inclinZero, 2);
    }
    else if (cmd == "STATUS") {
      Serial.print("[STATUS] BLE:"); Serial.print(bleActive ? "ON" : "OFF");
      Serial.print(",CONN:"); Serial.print(Bluefruit.connected() ? "YES" : "NO");
      Serial.print(",PF:"); Serial.print(paceFactor, 3);
      Serial.print(",IO:"); Serial.print(inclinOffset, 2);
      Serial.print(",IZ:"); Serial.print(inclinZero, 2);
      Serial.print(",IS:"); Serial.print(inclinSign);
      Serial.print(",IMU:"); Serial.print(imuOk ? "OK" : "FAIL");
      Serial.print(",LOOP_MAX:"); Serial.print(maxLoopTime);
      Serial.println("ms");
    }
    else if (cmd == "DIAG") {
      Serial.print("[DIAG] max_loop="); Serial.print(maxLoopTime);
      Serial.print("ms, imu_fails="); Serial.print(imuFailCount);
      Serial.print(", loops="); Serial.println(loopCount);
      maxLoopTime = 0;
      loopCount = 0;
    }
    else if (cmd == "IMU") {
      sensors_event_t event;
      accel.getEvent(&event);
      Serial.print("[IMU] accel x="); Serial.print(event.acceleration.x, 2);
      Serial.print(" y="); Serial.print(event.acceleration.y, 2);
      Serial.print(" z="); Serial.print(event.acceleration.z, 2);
      Serial.print(" | raw="); Serial.print(inclinaisonRaw, 2);
      Serial.print(" smooth="); Serial.print(inclinSmooth, 2);
      Serial.print(" zero="); Serial.print(inclinZero, 2);
      Serial.print(" sign="); Serial.print(inclinSign);
      Serial.print(" final="); Serial.println(inclinaisonY, 2);
    }
  }
}

// ==============================
// Serial Data to Pi
// ==============================
void sendSerialData() {
  float corrSpeedMs = smoothSpeedMs * paceFactor;
  uint8_t cadence = computeCadence(corrSpeedMs);

  Serial.print("$DATA,");
  Serial.print(speedKmhRaw, 2);     Serial.print(",");
  Serial.print(smoothSpeedMs, 4);   Serial.print(",");
  Serial.print(cadence);            Serial.print(",");
  Serial.print(distanceM, 2);       Serial.print(",");
  Serial.print(inclinaisonY, 1);    Serial.print(",");
  Serial.print(pulseCount);         Serial.print(",");
  Serial.print(bleActive ? 1 : 0);  Serial.print(",");
  Serial.print(Bluefruit.connected() ? 1 : 0); Serial.print(",");
  Serial.println(inclinSmooth, 2);  // Raw smooth before zero/sign
}

// ==============================
// SETUP
// ==============================
void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println("[BOOT] Treadmill Sensor v11 (LSM303)");

  pinMode(HALL_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(HALL_PIN), hallISR, FALLING);
  Serial.println("[OK] DRV5023");

  Wire.begin();
  Wire.setClock(100000);

  // Verifier si le LSM303 repond sur I2C (adresse accel = 0x19)
  Wire.beginTransmission(0x19);
  uint8_t i2cErr = Wire.endTransmission();
  if (i2cErr != 0) {
    Serial.print("[WARN] LSM303 not found on I2C (err=");
    Serial.print(i2cErr);
    Serial.println(") - check wiring (VIN, GND, SDA, SCL)");
    imuOk = false;
  } else if (!accel.begin()) {
    Serial.println("[WARN] LSM303 begin failed");
    imuOk = false;
  } else {
    accel.setRange(LSM303_RANGE_2G);
    accel.setMode(LSM303_MODE_NORMAL);
    Serial.println("[OK] LSM303");
  }

  setupBLE();
  Serial.println("[OK] BLE");

  Serial.println("[BOOT] READY");
}

// ==============================
// LOOP
// ==============================
void loop() {
  unsigned long loopStart = millis();
  unsigned long now = loopStart;

  processSerialCommand();
  updateSpeed();

  // IMU: toutes les 300ms
  if (imuOk && (now - lastImuRead >= IMU_READ_MS)) {
    lastImuRead = now;
    readIMU();
  }

  // BLE notification
  if (bleActive && (now - lastBle >= BLE_UPDATE_MS)) {
    lastBle = now;
    sendRscData();
  }

  // Serial au Pi
  if (now - lastSerial >= SERIAL_UPDATE_MS) {
    lastSerial = now;
    sendSerialData();
  }

  // Loop timing
  unsigned long loopTime = millis() - loopStart;
  if (loopTime > maxLoopTime) maxLoopTime = loopTime;
  loopCount++;
}
