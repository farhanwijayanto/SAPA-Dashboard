/*
 * SAPA Gate Controller (ESP32) — versi dengan HC-SR04 Ultrasonic
 *
 * Hardware:
 *   - Servo motor on GPIO 13 (PWM)
 *   - Active buzzer on GPIO 14
 *   - HC-SR04 ultrasonic: TRIG GPIO 26, ECHO GPIO 25
 *   - LED indicator on GPIO 2
 *
 * Perbedaan dengan versi PIR:
 *   - Ultrasonic memberikan jarak (cm). Gate auto-close hanya ketika
 *     orang benar-benar lewat (jarak < TRIGGER_DISTANCE_CM lalu kembali
 *     ke jarak baseline). Lebih akurat dari PIR yang hanya True/False.
 *   - Topic MQTT publish: sapa/ultrasonic (dengan field "distance_cm").
 *
 * MQTT topics (sama dengan versi PIR + tambahan):
 *   sapa/gate              (subscribe)
 *   sapa/device/heartbeat  (publish)
 *   sapa/ultrasonic        (publish) -> {"distance_cm": 45.2, "person_detected": true}
 *   sapa/pir               (publish) -> dipertahankan untuk kompatibilitas
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// --- Sesuaikan dengan environment Anda ---
const char* WIFI_SSID     = "YOUR_WIFI";
const char* WIFI_PASS     = "YOUR_PASSWORD";
const char* MQTT_HOST     = "sapa.farhn.dev";
const uint16_t MQTT_PORT  = 31883;
const char* MQTT_USER     = "esp32";
const char* MQTT_PASS     = "<password user esp32 dari mosquitto_passwd>";
const char* MQTT_CLIENT   = "sapa-esp32-gate";

const char* TOPIC_GATE        = "sapa/gate";
const char* TOPIC_HB          = "sapa/device/heartbeat";
const char* TOPIC_ULTRASONIC  = "sapa/ultrasonic";

const int PIN_SERVO     = 13;
const int PIN_BUZZER    = 14;
const int PIN_TRIG      = 26;
const int PIN_ECHO      = 25;
const int PIN_LED       = 2;

const int CLOSED_ANGLE  = 0;
const int OPEN_ANGLE    = 90;

// Threshold deteksi: kalau jarak < TRIGGER_DISTANCE_CM artinya ada orang.
// Sesuaikan dengan instalasi gate Anda.
const float TRIGGER_DISTANCE_CM = 60.0;
// Baseline: jarak normal saat tidak ada orang. Dipakai untuk deteksi
// "orang sudah lewat" (jarak kembali besar).
const float BASELINE_DISTANCE_CM = 150.0;

const unsigned long AUTO_CLOSE_MS  = 5000;
const unsigned long HEARTBEAT_MS   = 5000;
const unsigned long ULTRASONIC_INTERVAL_MS = 100;  // baca tiap 100ms
const unsigned long BEEP_ON_MS     = 120;
const unsigned long BEEP_OFF_MS    = 120;
const unsigned long INVALID_BEEPS  = 2;

WiFiClient netClient;
PubSubClient mqtt(netClient);
Servo gateServo;

bool gateOpen = false;
unsigned long openedAt = 0;
unsigned long lastHeartbeat = 0;
unsigned long lastUltrasonicRead = 0;
bool personDetected = false;
float lastDistance = -1.0;

float readUltrasonicCm() {
  // Trigger pulse: 10us HIGH
  digitalWrite(PIN_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG, LOW);

  // Read echo: pulseIn returns microseconds
  unsigned long duration = pulseIn(PIN_ECHO, HIGH, 30000);  // timeout 30ms (~5m)
  if (duration == 0) {
    return -1.0;  // no echo
  }
  // Speed of sound ~343 m/s -> 0.0343 cm/us, divided by 2 (round trip)
  return duration * 0.0343 / 2.0;
}

void publishUltrasonic(float distance_cm, bool detected) {
  StaticJsonDocument<128> doc;
  doc["distance_cm"] = distance_cm;
  doc["person_detected"] = detected;
  doc["gate_open"] = gateOpen;
  char buf[128];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_ULTRASONIC, (uint8_t*)buf, n, false);
}

void publishHeartbeat() {
  StaticJsonDocument<128> doc;
  doc["online"] = true;
  doc["source"] = "esp32";
  doc["gate_open"] = gateOpen;
  char buf[128];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_HB, (uint8_t*)buf, n, false);
}

void closeGate() {
  gateServo.write(CLOSED_ANGLE);
  gateOpen = false;
  digitalWrite(PIN_LED, LOW);
  Serial.println("Gate closed");
}

void openGate() {
  gateServo.write(OPEN_ANGLE);
  gateOpen = true;
  openedAt = millis();
  personDetected = false;  // reset state
  digitalWrite(PIN_LED, HIGH);
  digitalWrite(PIN_BUZZER, HIGH);
  delay(BEEP_ON_MS);
  digitalWrite(PIN_BUZZER, LOW);
  Serial.println("Gate opened");
}

void invalidBeep() {
  for (unsigned long i = 0; i < INVALID_BEEPS; i++) {
    digitalWrite(PIN_BUZZER, HIGH);
    delay(BEEP_ON_MS);
    digitalWrite(PIN_BUZZER, LOW);
    delay(BEEP_OFF_MS);
  }
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  if (strcmp(topic, TOPIC_GATE) != 0) return;
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, payload, length);
  if (err) {
    Serial.println("Bad JSON on sapa/gate");
    invalidBeep();
    return;
  }
  const char* action = doc["action"] | "";
  Serial.print("Received action=");
  Serial.println(action);
  if (strcmp(action, "open") == 0) {
    openGate();
  } else if (strcmp(action, "close") == 0) {
    closeGate();
  } else if (strcmp(action, "invalid") == 0) {
    closeGate();
    invalidBeep();
  } else {
    invalidBeep();
  }
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(300);
    Serial.print('.');
  }
  Serial.print(" WiFi OK ");
  Serial.println(WiFi.localIP());
}

void connectMqtt() {
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(onMqttMessage);
  while (!mqtt.connected()) {
    Serial.print("MQTT...");
    bool ok = strlen(MQTT_USER) > 0
      ? mqtt.connect(MQTT_CLIENT, MQTT_USER, MQTT_PASS)
      : mqtt.connect(MQTT_CLIENT);
    if (ok) {
      Serial.println("OK");
      mqtt.subscribe(TOPIC_GATE);
      publishHeartbeat();
    } else {
      Serial.print(" rc=");
      Serial.println(mqtt.state());
      delay(2000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_TRIG, OUTPUT);
  pinMode(PIN_ECHO, INPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  digitalWrite(PIN_LED, LOW);
  digitalWrite(PIN_TRIG, LOW);

  gateServo.setPeriodHertz(50);
  gateServo.attach(PIN_SERVO, 500, 2500);
  closeGate();

  connectWifi();
  connectMqtt();
}

void loop() {
  if (!mqtt.connected()) {
    connectMqtt();
  }
  mqtt.loop();

  // Baca ultrasonic tiap 100ms
  if (millis() - lastUltrasonicRead > ULTRASONIC_INTERVAL_MS) {
    float distance = readUltrasonicCm();
    lastUltrasonicRead = millis();
    if (distance > 0) {
      lastDistance = distance;

      // Logika auto-close pakai ultrasonic
      if (gateOpen) {
        if (!personDetected && distance < TRIGGER_DISTANCE_CM) {
          // Orang baru saja masuk zona deteksi
          personDetected = true;
          publishUltrasonic(distance, true);
          Serial.print("Person detected at ");
          Serial.print(distance);
          Serial.println(" cm");
        } else if (personDetected && distance > BASELINE_DISTANCE_CM) {
          // Orang sudah lewat -> tutup gate
          publishUltrasonic(distance, false);
          Serial.println("Person passed, closing gate");
          delay(300);
          closeGate();
        } else if (millis() - openedAt > AUTO_CLOSE_MS) {
          // Fallback timeout (kalau ultrasonic miss)
          Serial.println("Auto-close timeout");
          closeGate();
        }
      }
    }
  }

  // Heartbeat berkala
  if (millis() - lastHeartbeat > HEARTBEAT_MS) {
    publishHeartbeat();
    lastHeartbeat = millis();
  }
}
