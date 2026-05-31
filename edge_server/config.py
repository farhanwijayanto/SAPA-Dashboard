"""Edge server configuration loaded from environment / .env.

PENTING soal SAPA_API_BASE:
  - Untuk konek ke VPS produksi: gunakan  https://sapa.farhn.dev/api
    (HARUS pakai /api di belakang, karena ingress route /api -> backend)
  - Untuk test lokal docker-compose:     http://localhost:8080
"""
from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv  # type: ignore
    # Muat .env yang ada di folder edge_server/ apapun current working dir-nya
    _here = os.path.dirname(os.path.abspath(__file__))
    _env_path = os.path.join(_here, ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
    else:
        load_dotenv()
except Exception:
    pass


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except Exception:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except Exception:
        return default


@dataclass
class Settings:
    api_base: str
    api_token: str
    edge_ingest_key: str
    mqtt_broker: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_topic_gate: str
    mqtt_topic_heartbeat: str
    mqtt_topic_pir: str
    camera_index: int
    recognition_threshold: float
    cooldown_seconds: int
    sync_every_seconds: int
    frame_push_fps: int
    # Username manager untuk login otomatis (dapat JWT yang tidak expired
    # selama service hidup). Dipakai untuk GET /employees/ saat sync foto.
    sapa_username: str
    sapa_password: str


def load_settings() -> Settings:
    return Settings(
        # Default ke produksi supaya tidak salah konek ke localhost.
        api_base=_get("SAPA_API_BASE", "https://sapa.farhn.dev/api").rstrip("/"),
        api_token=_get("SAPA_API_TOKEN"),
        edge_ingest_key=_get("EDGE_INGEST_KEY"),
        mqtt_broker=_get("MQTT_BROKER", "sapa.farhn.dev"),
        mqtt_port=_get_int("MQTT_PORT", 31883),
        mqtt_username=_get("MQTT_USERNAME", "edge"),
        mqtt_password=_get("MQTT_PASSWORD"),
        mqtt_topic_gate=_get("MQTT_TOPIC_GATE", "sapa/gate"),
        mqtt_topic_heartbeat=_get("MQTT_TOPIC_HEARTBEAT", "sapa/device/heartbeat"),
        mqtt_topic_pir=_get("MQTT_TOPIC_PIR", "sapa/pir"),
        camera_index=_get_int("CAMERA_INDEX", 0),
        recognition_threshold=_get_float("RECOGNITION_THRESHOLD", 0.55),
        cooldown_seconds=_get_int("COOLDOWN_SECONDS", 5),
        sync_every_seconds=_get_int("SYNC_EVERY_SECONDS", 60),
        frame_push_fps=max(1, _get_int("FRAME_PUSH_FPS", 2)),
        sapa_username=_get("SAPA_USERNAME", "manager"),
        sapa_password=_get("SAPA_PASSWORD"),
    )
