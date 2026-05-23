# SAPA Edge Server

Python service that runs on a local edge node (PC, mini-PC, Raspberry Pi 4/5, Jetson Nano, etc.) near the gate.

Responsibilities:
- Capture video from the local USB / CSI camera.
- Push live frames to the VPS dashboard via `POST /api/edge/frame` (so dashboard live preview pulls from /edge).
- Run **face recognition** locally (the AI model lives on the edge, not on the VPS).
- On a confident match, send the result to the VPS via `POST /api/edge/face-match`. Backend logs the attendance to MongoDB and publishes `sapa/gate -> {action: open}` over MQTT to the ESP32.
- On unknown / invalid face, send `is_valid=false` so backend triggers buzzer (`sapa/gate -> {action: invalid}`).

This service replaces the simple browser-based `/edge` page with a real recognizer; the browser `/edge` page is still useful for installations without a Python-capable edge device (it just publishes frames; backend won't auto-open the gate in that case).

## How it links the VPS `/edge` path with the AI model on the edge

- The VPS owns the data (employee profile + reference face image at `/api/uploads/faces/<id>.jpg`).
- The edge service calls `GET /api/employees/` and `GET /api/uploads/faces/<id>.jpg` periodically to **sync embeddings** to a local cache (`embeddings.npz`).
- All face matching runs on the edge using the cached embeddings; only the *result* travels back to the VPS.
- The browser `/edge` page on the VPS only shows the live frames and the latest verification result returned by the edge.

## Architecture

```
┌────────────────────────┐    HTTPS     ┌────────────────────┐
│  Edge Server (this)    │ ───────────▶ │  VPS Backend (API) │
│  - camera capture      │              │  - /edge/frame     │
│  - face recognition    │              │  - /edge/face-match│
│  - MQTT pub/sub        │              │  - MongoDB log     │
└─────────┬──────────────┘              └─────────┬──────────┘
          │ MQTT (LAN)                            │ MQTT publish
          ▼                                       ▼
       ┌────────┐                         ┌──────────────┐
       │ ESP32  │   subscribe sapa/gate   │ Mosquitto    │
       │ servo, │◀───────────────────────│ (k8s/VPS LAN) │
       │ buzzer,│   publish sapa/pir      │              │
       │ PIR    │───────────────────────▶│              │
       └────────┘                         └──────────────┘
```

## Install

```bash
cd edge_server
python -m venv venv
./venv/bin/pip install -r requirements.txt
```

`face_recognition` requires `dlib`. On Linux:
```bash
sudo apt install -y cmake build-essential libopenblas-dev liblapack-dev
```

On Raspberry Pi consider the lighter `mediapipe` alternative — see `recognizer.py` notes.

## Configure

Copy `.env.example` to `.env`:

```
SAPA_API_BASE=https://sapa.example.com/api
SAPA_API_TOKEN=<a token from a service account login>
EDGE_INGEST_KEY=<must match backend EDGE_INGEST_KEY>
MQTT_BROKER=192.168.1.10
MQTT_PORT=1883
MQTT_USERNAME=esp32
MQTT_PASSWORD=changeme
CAMERA_INDEX=0
RECOGNITION_THRESHOLD=0.55
COOLDOWN_SECONDS=5
SYNC_EVERY_SECONDS=60
```

## Run

```bash
./venv/bin/python -m edge_server.main
```

It will:
1. Sync employee face embeddings from the VPS.
2. Open the camera and start streaming frames to the VPS (still keeps `/edge` dashboard frame fresh).
3. Match faces locally. On confident match, call `POST /api/edge/face-match` with `{is_valid, employee_id, confidence, direction}`.
4. Publish heartbeat to `sapa/device/heartbeat` so the dashboard knows the gate device is online.
