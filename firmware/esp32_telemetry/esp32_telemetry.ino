// RescueSight ESP32 telemetry relay  (UNTESTED ON HARDWARE - bring-up checklist in docs/hardware.md)
//
// Wiring (ESP32 DevKit):
//   NEO-M8N GPS   TX -> GPIO16 (RX2), RX -> GPIO17 (TX2), 5V/3V3, GND
//   QMC5883L      SDA -> GPIO21, SCL -> GPIO22   (compass for heading; mount away from motors/ESCs)
//   MPU6050       SDA -> GPIO21, SCL -> GPIO22   (pitch/roll; optional, sends 0 if absent)
//   USB           -> Jetson (shows up as /dev/ttyUSB0 or /dev/ttyACM0)
//
// Output: one JSON line per sample at 10 Hz on USB serial, 115200 baud:
//   {"t":1794668400.120,"lat":42.3890100,"lon":-72.5283000,"alt_agl":24.0,"hdg":91.0,
//    "pitch":0.8,"roll":-1.2,"fix":3,"sats":11,"hdop":0.9}
//
// alt_agl = GPS altitude minus the altitude recorded at the first good fix (i.e. on the
// ground before takeoff). GPS altitude is noisy (several metres); a barometer or a
// downward rangefinder (e.g. TF-Luna) is a big accuracy upgrade - add it to altAgl().
//
// Libraries (Arduino Library Manager): TinyGPSPlus, QMC5883LCompass, Adafruit MPU6050.

#include <Wire.h>
#include <TinyGPSPlus.h>
#include <QMC5883LCompass.h>
#include <Adafruit_MPU6050.h>
#include <time.h>

TinyGPSPlus gps;
HardwareSerial GPSSerial(2);
QMC5883LCompass compass;
Adafruit_MPU6050 mpu;
bool haveMpu = false;

double homeAlt = NAN;            // metres MSL at first good fix
float declinationDeg = -14.0;    // magnetic declination; Amherst MA is about -14 deg (west)
uint32_t lastFixMillis = 0;      // millis() when the last GPS time was decoded
double lastFixEpoch = 0;         // GPS UTC epoch seconds at lastFixMillis

double gpsEpoch() {
  if (!gps.date.isValid() || !gps.time.isValid()) return 0;
  struct tm t = {};
  t.tm_year = gps.date.year() - 1900; t.tm_mon = gps.date.month() - 1; t.tm_mday = gps.date.day();
  t.tm_hour = gps.time.hour(); t.tm_min = gps.time.minute(); t.tm_sec = gps.time.second();
  setenv("TZ", "UTC0", 1); tzset();
  return (double)mktime(&t) + gps.time.centisecond() / 100.0;
}

float headingDeg() {
  compass.read();
  float h = atan2((float)compass.getY(), (float)compass.getX()) * 180.0 / PI + declinationDeg;
  // Mounting: rotate here so 0 = drone nose pointing true north. Verify on the bench!
  while (h < 0) h += 360; while (h >= 360) h -= 360;
  return h;
}

void attitude(float &pitch, float &roll) {
  pitch = roll = 0;
  if (!haveMpu) return;
  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);
  // accelerometer-only tilt: fine for hover/slow flight, noisy under acceleration
  roll  = atan2(a.acceleration.y, a.acceleration.z) * 180.0 / PI;
  pitch = atan2(-a.acceleration.x, sqrt(a.acceleration.y * a.acceleration.y + a.acceleration.z * a.acceleration.z)) * 180.0 / PI;
}

double altAgl() {
  if (!gps.altitude.isValid() || isnan(homeAlt)) return -1;
  return gps.altitude.meters() - homeAlt;
}

void setup() {
  Serial.begin(115200);
  GPSSerial.begin(9600, SERIAL_8N1, 16, 17);   // NEO-M8N default 9600; raise to 10 Hz with u-center if possible
  Wire.begin(21, 22);
  compass.init();
  haveMpu = mpu.begin();
}

void loop() {
  while (GPSSerial.available()) {
    if (gps.encode(GPSSerial.read()) && gps.time.isUpdated()) {
      double e = gpsEpoch();
      if (e > 0) { lastFixEpoch = e; lastFixMillis = millis(); }
    }
  }
  static uint32_t lastOut = 0;
  if (millis() - lastOut < 100) return;      // 10 Hz output
  lastOut = millis();

  bool fixOk = gps.location.isValid() && gps.location.age() < 2000 && gps.satellites.value() >= 5;
  if (fixOk && isnan(homeAlt) && gps.altitude.isValid()) homeAlt = gps.altitude.meters();
  if (lastFixEpoch == 0) return;              // no GPS time yet: Jetson can't align frames

  float pitch, roll;
  attitude(pitch, roll);
  double t = lastFixEpoch + (millis() - lastFixMillis) / 1000.0;
  double hdop = gps.hdop.isValid() ? gps.hdop.hdop() : 99.0;

  Serial.printf("{\"t\":%.3f,\"lat\":%.7f,\"lon\":%.7f,\"alt_agl\":%.2f,\"hdg\":%.1f,"
                "\"pitch\":%.1f,\"roll\":%.1f,\"fix\":%d,\"sats\":%d,\"hdop\":%.1f}\n",
                t, gps.location.lat(), gps.location.lng(), altAgl(), headingDeg(),
                pitch, roll, fixOk ? 3 : 0, (int)gps.satellites.value(), hdop);
}
