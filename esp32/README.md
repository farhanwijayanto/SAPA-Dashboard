# SAPA ESP32 Gate Firmware

A reference firmware for the gate controller (servo + buzzer + PIR), written for Arduino ESP32 core. Behaves like a KAI Access gate: opens for valid passes, beeps on invalid, auto-closes after the PIR sees motion.

## Hardware

| Component | Pin |
|-----------|-----|
| SG90 / MG90S Servo (signal) | GPIO 13 |
| Active buzzer (+) | GPIO 14 |
| HC-SR501 PIR (OUT) | GPIO 27 |
| Power 5V to servo & PIR | external 5V (NOT from ESP32 3.3V) |
| Common GND | yes |
| Status LED | GPIO 2 (built-in) |

> SG90 servos can spike >500mA when moving — give them their own 5V supply with a shared ground.

## Libraries (Arduino IDE / PlatformIO)

- `WiFi` (built-in)
- `PubSubClient`
- `ArduinoJson`
- `ESP32Servo`

## Configure

Edit the constants at the top of `sapa_gate.ino`:

```cpp
const char* WIFI_SSID = "...";
const char* WIFI_PASS = "...";
const char* MQTT_HOST = "192.168.1.10";   // VPS LAN IP / broker
const char* MQTT_USER = "esp32";
const char* MQTT_PASS = "changeme";
```

## MQTT contract

- subscribes `sapa/gate`
- publishes `sapa/device/heartbeat` every 5s (so the dashboard knows the device is online)
- publishes `sapa/pir` on motion (so the backend can also auto-close server-side)

Payloads accepted on `sapa/gate`:

```json
{"action": "open",    "employee_id": "123456"}
{"action": "close"}
{"action": "invalid", "reason": "unknown_face"}
```
