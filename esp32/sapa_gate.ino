/*
 * SAPA Gate Controller (ESP32)
 *
 * Hardware:
 *   - Servo motor on GPIO 13 (PWM)         -> opens/closes the gate arm
 *   - Active buzzer on GPIO 14             -> beep on valid, double-beep on invalid
 *   - PIR motion sensor on GPIO 27         -> detects pass-through to auto-close
 *   - LED indicator on GPIO 2 (built-in)
 *
 * MQTT topics:
 *   sapa/gate                  (subscribe) -> {action: open|close|invalid, employee_id?, reason?}
 *   sapa/attendance            (publish)   -> presence events from local readers (optional)
 *   sapa/device/heartbeat      (publish)   -> {online: true, source: "esp32"} every 5s
 *   sapa/pir                   (publish)   -> {motion: true} on motion event
 *
 * Behavior:
 *   - {action: "open"}    : rotate servo to OPEN_ANGLE, beep once, hold open until
 *                           PIR detects motion or AUTO_CLOSE_MS elapses; then close.
 *   - {action: "close"}   : force servo to CLOSED_ANGLE.
 *   - {action: "invalid"} : keep servo CLOSED, double-beep buzzer.
 *
 * Like KAI Access: gate only opens for valid users, beeps on invalid, and the gate
 * arm closes after the PIR sees the person pass through.
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <ESP32Servo.h>

// --- Customize these for your environment ---
const char* WIFI_SSID     = "YOUR_WIFI";
const char* WIFI_PASS     = "YOUR_PASSWORD";
const char* MQTT_HOST     = "192.168.1.10";  // VPS LAN IP or local broker
const uint16_t MQTT_PORT  = 1883;
const char* MQTT_USER     = "esp32";
const char* MQTT_PASS     = "changeme";
const char* MQTT_CLIENT   = "sapa-esp32-gate";

const char* TOPIC_GATE    = "sapa/gate";
const char* TOPIC_HB      = "sapa/device/heartbeat";
const char* TOPIC_PIR     = "sapa/pir";

const int PIN_SERVO       = 13;
const int PIN_BUZZER      = 14;
const int PIN_PIR         = 27;
const int PIN_LED         = 2;

const int CLOSED_ANGLE    = 0;
const int OPEN_ANGLE      = 90;
const unsigned long AUTO_CLOSE_MS  = 5000;     // fall-back close if PIR misses
const unsigned long HEARTBEAT_MS   = 5000;
const unsigned long INVALID_BEEPS  = 2;
const unsigned long BEEP_ON_MS     = 120;
const unsigned long BEEP_OFF_MS    = 120;

WiFiClient netClient;
PubSubClient mqtt(netClient);
Servo gateServo;

bool gateOpen = false;
unsigned long openedAt = 0;
unsigned long lastHeartbeat = 0;

void setServoAngle(int angle) {
  gateServo.write(angle);
}

void closeGate() {
  setServoAngle(CLOSED_ANGLE);
  gateOpen = false;
  digitalWrite(PIN_LED, LOW);
}

void openGate() {
  setServoAngle(OPEN_ANGLE);
  gateOpen = true;
  openedAt = millis();
  digitalWrite(PIN_LED, HIGH);
  // single happy beep
  digitalWrite(PIN_BUZZER, HIGH);
  delay(BEEP_ON_MS);
  digitalWrite(PIN_BUZZER, LOW);
}

void invalidBeep() {
  for (unsigned long i = 0; i < INVALID_BEEPS; i++) {
    digitalWrite(PIN_BUZZER, HIGH);
    delay(BEEP_ON_MS);
    digitalWrite(PIN_BUZZER, LOW);
    delay(BEEP_OFF_MS);
  }
}

void publishPirMotion() {
  StaticJsonDocument<128> doc;
  doc["motion"] = true;
  doc["ts"] = millis();
  char buf[128];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  mqtt.publish(TOPIC_PIR, (uint8_t*)buf, n, false);
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
    Serial.print("MQTT…");
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
  pinMode(PIN_PIR, INPUT);
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);
  digitalWrite(PIN_LED, LOW);

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

  // PIR-triggered auto-close
  if (gateOpen) {
    int pir = digitalRead(PIN_PIR);
    if (pir == HIGH) {
      publishPirMotion();
      delay(300);                 // let the person clear
      closeGate();
    } else if (millis() - openedAt > AUTO_CLOSE_MS) {
      closeGate();
    }
  }

  // periodic heartbeat
  if (millis() - lastHeartbeat > HEARTBEAT_MS) {
    publishHeartbeat();
    lastHeartbeat = millis();
  }
}
