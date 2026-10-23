/*
  Dual-Tracker node firmware (ESP32 DevKit, 30-pin)

  Reads:
    - INMP441 I2S microphone  -> sound level (dB, relative) + activity (0..1)
    - DS18B20 temperature x1 or x2 -> room temperature, window/wall ("edge") temperature
  Sends one JSON reading to the laptop over Wi-Fi every SEND_EVERY_MS.
  Raw audio never leaves the board: only the numbers below are sent.

  Wiring
    INMP441  VDD->3V3  GND->GND  L/R->GND  SCK->D32  WS->D25  SD->D33
    DS18B20  red->3V3  black->GND  data->D4, 4.7k resistor between data and 3V3
             (both probes share the same three rows)

  Libraries: OneWire, DallasTemperature. Board: "ESP32 Dev Module" (esp32 by Espressif, v3.x).
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ESP_I2S.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// ------------------------------------------------------------------ settings to edit
const char* WIFI_SSID     = "name";
const char* WIFI_PASSWORD = "password";
// Laptop's IP on the hotspot (Mac: System Settings > Wi-Fi > Details > IP address)
const char* SERVER_URL    = "http://ip_adress:8000/readings";
const char* DEVICE_ID     = "node-1";
const char* ROOM_ID       = "A-101";
const bool  SWAP_SENSORS  = false;   // set true if "room" and "edge" come out the wrong way round

const uint32_t SEND_EVERY_MS = 2000;

// ------------------------------------------------------------------ pins
const int PIN_ONEWIRE = 4;
const int PIN_I2S_SCK = 32;
const int PIN_I2S_WS  = 25;
const int PIN_I2S_SD  = 33;

// ------------------------------------------------------------------ sound
const uint32_t SAMPLE_RATE = 16000;
const size_t   FRAME = 800;             // 50 ms of audio per frame
I2SClass i2s;
int32_t frameBuf[FRAME];
const float SOUND_OFFSET_DB = -55.0;   // shifts the displayed level only

float noiseFloorDb = 40.0f;             // adapts to the room's background hum
bool  floorInit = false;
const float ACTIVE_MARGIN_DB = 6.0f;    // frame counts as "active" this far above the floor

// accumulated over one send period
double sumDb = 0;
uint32_t frames = 0, activeFrames = 0;
float peakDb = 0;

// ------------------------------------------------------------------ temperature
OneWire oneWire(PIN_ONEWIRE);
DallasTemperature temps(&oneWire);
int tempCount = 0;
uint32_t lastTempRequest = 0;
float roomTemp = NAN, edgeTemp = NAN;

uint32_t lastSend = 0;

// One 50 ms frame -> level in dB (relative scale, not calibrated SPL).
bool readFrameDb(float& db) {
  size_t got = i2s.readBytes((char*)frameBuf, sizeof(frameBuf)) / sizeof(int32_t);
  if (got == 0) return false;
  double mean = 0;
  for (size_t i = 0; i < got; i++) mean += (frameBuf[i] >> 8);
  mean /= got;                                     // remove DC offset
  double sq = 0;
  for (size_t i = 0; i < got; i++) {
    double s = (frameBuf[i] >> 8) - mean;
    sq += s * s;
  }
  double rms = sqrt(sq / got);
  db = 20.0 * log10(rms + 1.0);
  return true;
}

uint32_t lastWifiTry = 0;

void connectWifi() {
  lastWifiTry = millis();
  if (WiFi.status() == WL_CONNECTED) return;

  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);          // drop any half-finished attempt first
  delay(300);

  // Scan first, so the Serial Monitor shows whether the hotspot is visible at all
  Serial.println("Wi-Fi: scanning for networks...");
  int n = WiFi.scanNetworks();
  bool found = false;
  for (int i = 0; i < n; i++) {
    Serial.printf("   %-24s ch %2d  %d dBm\n", WiFi.SSID(i).c_str(), WiFi.channel(i), WiFi.RSSI(i));
    if (WiFi.SSID(i) == WIFI_SSID) found = true;
  }
  WiFi.scanDelete();
  if (!found) {
    Serial.printf("Wi-Fi: \"%s\" is NOT in that list. Hotspot is off, on 5 GHz, or the name is wrong.\n", WIFI_SSID);
    return;                              // retry later, no point trying the password
  }

  Serial.printf("Wi-Fi: connecting to %s", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(400);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nWi-Fi: connected, ESP32 IP %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.printf("\nWi-Fi: found the hotspot but could not join (status %d). Most likely a wrong password.\n", WiFi.status());
  }
}

String num(float v, int digits) {
  return isnan(v) ? String("null") : String(v, digits);
}

void sendReading(float soundDb, float activity) {
  String json = String("{") +
    "\"device_id\":\"" + DEVICE_ID + "\"," +
    "\"room_id\":\"" + ROOM_ID + "\"," +
    "\"sound_db\":" + num(soundDb + SOUND_OFFSET_DB, 1) + "," +
    "\"activity\":" + num(activity, 3) + "," +
    "\"room_temp\":" + num(roomTemp, 2) + "," +
    "\"edge_temp\":" + num(edgeTemp, 2) + "}";

  Serial.println(json);

  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.setTimeout(1500);
  http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(json);
  if (code != 200) Serial.printf("  send failed: HTTP %d\n", code);
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\nDual-Tracker node starting");

  // microphone
  i2s.setPins(PIN_I2S_SCK, PIN_I2S_WS, -1, PIN_I2S_SD);
  if (!i2s.begin(I2S_MODE_STD, SAMPLE_RATE, I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_MONO, I2S_STD_SLOT_LEFT)) {
    Serial.println("MIC: I2S failed to start. Check the SCK/WS/SD wires.");
  }

  // temperature sensors
  temps.begin();
  temps.setResolution(11);              // 0.125 C steps, ~375 ms per conversion
  temps.setWaitForConversion(false);    // don't block the microphone while converting
  tempCount = temps.getDeviceCount();
  Serial.printf("TEMP: found %d DS18B20 sensor(s)\n", tempCount);
  if (tempCount == 0) Serial.println("TEMP: none found. Check red/black/data wires and the 4.7k resistor.");
  temps.requestTemperatures();
  lastTempRequest = millis();

  connectWifi();
}

void loop() {
  // 1) sound: one 50 ms frame per loop
  float db;
  if (readFrameDb(db)) {
    // start the noise floor at the first thing we hear (starting at a fixed 40 dB made
    // every frame look "active" for minutes)
    if (!floorInit) { noiseFloorDb = db; floorInit = true; }
    // noise floor drifts down quickly, up slowly, so speech doesn't drag it up
    if (db < noiseFloorDb) noiseFloorDb = 0.9f * noiseFloorDb + 0.1f * db;
    else noiseFloorDb = 0.998f * noiseFloorDb + 0.002f * db;
    sumDb += db;
    frames++;
    if (db > noiseFloorDb + ACTIVE_MARGIN_DB) activeFrames++;
    if (db > peakDb) peakDb = db;
  }

  // 2) temperatures: read the finished conversion, start the next one
  if (millis() - lastTempRequest > 400) {
    float t0 = tempCount > 0 ? temps.getTempCByIndex(0) : DEVICE_DISCONNECTED_C;
    float t1 = tempCount > 1 ? temps.getTempCByIndex(1) : DEVICE_DISCONNECTED_C;
    float a = (t0 == DEVICE_DISCONNECTED_C || t0 == 85.0f) ? NAN : t0;
    float b = (t1 == DEVICE_DISCONNECTED_C || t1 == 85.0f) ? NAN : t1;
    roomTemp = SWAP_SENSORS ? b : a;
    edgeTemp = SWAP_SENSORS ? a : b;
    temps.requestTemperatures();
    lastTempRequest = millis();
  }

  // 3) every SEND_EVERY_MS: summarise and send
  if (millis() - lastSend >= SEND_EVERY_MS) {
    lastSend = millis();
    float avgDb = frames ? sumDb / frames : NAN;
    float activity = frames ? (float)activeFrames / frames : NAN;
    sendReading(avgDb, activity);
    sumDb = 0; frames = 0; activeFrames = 0; peakDb = 0;
  }

  // 4) Wi-Fi dropped or never joined: retry every 20 s (not on every send)
  if (WiFi.status() != WL_CONNECTED && millis() - lastWifiTry > 20000) connectWifi();
}
