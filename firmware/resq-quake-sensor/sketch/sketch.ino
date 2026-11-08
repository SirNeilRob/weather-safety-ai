// ResQ quake sensor: ADXL345 on the UNO Q header pins (3.3V, GND, SDA, SCL).
//
// The microcontroller does the fast part: it samples the accelerometer at
// 100 Hz, removes gravity, and turns a burst of shaking into ONE event made of
// two numbers, the peak acceleration (g) and how long the shaking lasted (s).
// Only that pair crosses the Bridge to the Linux side, where
// python/main.py runs the sensor_bridge gates (noise floor, knock filter,
// cooldown, PGA -> MMI -> severity) and posts the alert to the backend.
//
//   ADXL345 --I2C--> this sketch --Bridge.notify("shake_event", peak, dur)--> python/main.py
//
// No ADXL345 library: the chip is configured with four register writes, which
// keeps the sketch dependency-free on the Zephyr core.

#include <Wire.h>
#include <Arduino_RouterBridge.h>

// ---------------------------------------------------------------- ADXL345
const uint8_t ADDRS[] = {0x53, 0x1D};  // SDO low (most breakouts) / SDO high
const uint8_t REG_DEVID = 0x00;        // reads 0xE5 on a genuine ADXL345
const uint8_t REG_BW_RATE = 0x2C;
const uint8_t REG_POWER_CTL = 0x2D;
const uint8_t REG_DATA_FORMAT = 0x31;
const uint8_t REG_DATAX0 = 0x32;
const float G_PER_LSB = 0.0039f;       // full-resolution mode: 3.9 mg/LSB at any range

// ---------------------------------------------------------------- detection
const unsigned long SAMPLE_MS = 10;    // 100 Hz
const float TRIGGER_G = 0.02f;         // dynamic acceleration that opens an event
const unsigned long QUIET_MS = 1000;   // calm this long ends the event (hand shaking has short pauses)
const unsigned long MAX_EVENT_MS = 20000;
// Shaking is measured against a FAST moving average (~0.2 s at 100 Hz), never
// frozen. That makes it a vibration detector, not a tilt detector: if the board
// shifts or tilts, the average catches up within about half a second and the
// event closes, instead of reading the tilted share of gravity (up to 1 g) as
// endless shaking. Back-and-forth shaking (1-10 Hz) still passes through.
const float BASELINE_ALPHA = 0.05f;
const unsigned long STATUS_MS = 10000; // heartbeat to the Linux side

// Status codes sent with Bridge.notify("sensor_status", code, value)
const int STATUS_OK = 1;          // value = I2C address in use
const int STATUS_NOT_FOUND = 2;   // value = 0
const int STATUS_HEARTBEAT = 3;   // value = events detected so far

TwoWire *bus = nullptr;
uint8_t addr = 0;

float baseX = 0, baseY = 0, baseZ = 1.0f;
bool baselineReady = false;
int warmup = 0;

bool inEvent = false;
unsigned long eventStart = 0, lastAbove = 0;
float eventPeak = 0;
long eventCount = 0;

unsigned long lastSample = 0, lastStatus = 0;

bool writeReg(uint8_t reg, uint8_t value) {
  bus->beginTransmission(addr);
  bus->write(reg);
  bus->write(value);
  return bus->endTransmission() == 0;
}

int readReg(TwoWire *w, uint8_t a, uint8_t reg) {
  w->beginTransmission(a);
  w->write(reg);
  if (w->endTransmission(false) != 0) return -1;
  if (w->requestFrom(a, (uint8_t)1) != 1) return -1;
  return w->read();
}

// Try the header bus (Wire) first, then the Qwiic bus (Wire1), at both addresses.
bool findSensor() {
  TwoWire *buses[] = {&Wire, &Wire1};
  for (TwoWire *w : buses) {
    w->begin();
    for (uint8_t a : ADDRS) {
      if (readReg(w, a, REG_DEVID) == 0xE5) {
        bus = w;
        addr = a;
        return true;
      }
    }
  }
  return false;
}

bool configureSensor() {
  return writeReg(REG_BW_RATE, 0x0A)        // 100 Hz output data rate
      && writeReg(REG_DATA_FORMAT, 0x0B)    // full resolution, +/-16 g (a knock never clips)
      && writeReg(REG_POWER_CTL, 0x08);     // measurement mode
}

bool readAccel(float &x, float &y, float &z) {
  bus->beginTransmission(addr);
  bus->write(REG_DATAX0);
  if (bus->endTransmission(false) != 0) return false;
  if (bus->requestFrom(addr, (uint8_t)6) != 6) return false;
  uint8_t b[6];
  for (int i = 0; i < 6; i++) b[i] = bus->read();
  x = (int16_t)(b[0] | (b[1] << 8)) * G_PER_LSB;
  y = (int16_t)(b[2] | (b[3] << 8)) * G_PER_LSB;
  z = (int16_t)(b[4] | (b[5] << 8)) * G_PER_LSB;
  return true;
}

void setup() {
  Bridge.begin();
  while (!findSensor() || !configureSensor()) {
    Bridge.notify("sensor_status", STATUS_NOT_FOUND, 0);
    delay(2000);
  }
  Bridge.notify("sensor_status", STATUS_OK, (int)addr);
}

void closeEvent(unsigned long now) {
  float durationS = (lastAbove - eventStart) / 1000.0f;
  eventCount++;
  // Python decides whether this is a quake or a knock; the sketch only measures.
  Bridge.notify("shake_event", eventPeak, durationS);
  inEvent = false;
  eventPeak = 0;
}

void loop() {
  unsigned long now = millis();

  if (now - lastStatus >= STATUS_MS) {
    lastStatus = now;
    Bridge.notify("sensor_status", STATUS_HEARTBEAT, (int)eventCount);
  }

  if (now - lastSample < SAMPLE_MS) return;
  lastSample = now;

  float x, y, z;
  if (!readAccel(x, y, z)) return;

  // Shaking = how far this sample is from the recent average (a high-pass
  // filter). The average always keeps moving, so a tilt fades out on its own.
  if (!baselineReady) {
    baseX = x; baseY = y; baseZ = z;
    if (++warmup >= 100) baselineReady = true;  // 1 s to settle after boot
    return;
  }
  float dx = x - baseX, dy = y - baseY, dz = z - baseZ;
  float dynamicG = sqrtf(dx * dx + dy * dy + dz * dz);

  baseX += BASELINE_ALPHA * (x - baseX);
  baseY += BASELINE_ALPHA * (y - baseY);
  baseZ += BASELINE_ALPHA * (z - baseZ);

  if (dynamicG >= TRIGGER_G) {
    if (!inEvent) {
      inEvent = true;
      eventStart = now;
      eventPeak = 0;
    }
    lastAbove = now;
    if (dynamicG > eventPeak) eventPeak = dynamicG;
  }

  if (inEvent && (now - lastAbove >= QUIET_MS || now - eventStart >= MAX_EVENT_MS)) {
    closeEvent(now);
  }
}
