"""Edge server entry point.

Run with:  python -m edge_server.main

This binds the camera, the local face recognizer, the VPS API, and an MQTT
heartbeat into a single loop. Every camera frame is offered to the recognizer
and (at a lower rate) pushed to the VPS for the live dashboard preview.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import paho.mqtt.client as mqtt

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore

from .config import load_settings
from .recognizer import FaceRecognizer
from .sapa_client import SapaClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s")
logger = logging.getLogger("sapa.edge")


def _start_mqtt(settings) -> mqtt.Client:
    client = mqtt.Client(client_id=f"sapa-edge-{int(time.time())}")
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password or "")
    # Last-will so the VPS marks the gate offline if we crash
    client.will_set(
        settings.mqtt_topic_heartbeat,
        json.dumps({"online": False, "source": "edge"}),
        qos=1,
        retain=False,
    )

    def on_connect(_client, _userdata, _flags, rc):
        logger.info("MQTT connected rc=%s", rc)

    client.on_connect = on_connect
    try:
        client.connect(settings.mqtt_broker, settings.mqtt_port, 60)
    except Exception as exc:
        logger.warning("MQTT connect failed: %s", exc)
    client.loop_start()
    return client


def _heartbeat_loop(client: mqtt.Client, topic: str, stop: threading.Event):
    while not stop.is_set():
        try:
            client.publish(
                topic,
                json.dumps({"online": True, "source": "edge", "ts": time.time()}),
                qos=0,
                retain=False,
            )
        except Exception:
            pass
        stop.wait(5)


def _sync_loop(api: SapaClient, recognizer: FaceRecognizer, every: int, stop: threading.Event):
    while not stop.is_set():
        try:
            employees = api.list_employees()
            samples: list[tuple[str, bytes]] = []
            for emp in employees:
                emp_id = str(emp.get("id"))
                blob = api.get_face_image(emp_id)
                if blob:
                    samples.append((emp_id, blob))
            if samples:
                count = recognizer.rebuild(samples)
                logger.info("Synced %d employee faces -> %d embeddings", len(samples), count)
        except Exception as exc:
            logger.warning("Sync failed: %s", exc)
        stop.wait(every)


def _encode_jpeg(frame_bgr) -> Optional[bytes]:
    if cv2 is None:
        return None
    ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    if not ok:
        return None
    return buf.tobytes()


def main() -> int:
    settings = load_settings()
    if cv2 is None:
        logger.error("OpenCV (cv2) is required. Install requirements first.")
        return 1

    recognizer = FaceRecognizer(threshold=settings.recognition_threshold)
    recognizer.load_cache()

    api = SapaClient(settings)
    mqtt_client = _start_mqtt(settings)

    stop = threading.Event()
    threading.Thread(
        target=_heartbeat_loop,
        args=(mqtt_client, settings.mqtt_topic_heartbeat, stop),
        daemon=True,
    ).start()
    threading.Thread(
        target=_sync_loop,
        args=(api, recognizer, settings.sync_every_seconds, stop),
        daemon=True,
    ).start()

    cap = cv2.VideoCapture(settings.camera_index)
    if not cap.isOpened():
        logger.error("Failed to open camera index %s", settings.camera_index)
        return 2

    last_match_at = 0.0
    last_employee: Optional[str] = None
    frame_push_interval = 1.0 / settings.frame_push_fps
    last_frame_push = 0.0

    logger.info("Edge server running. threshold=%.2f cooldown=%ds", settings.recognition_threshold, settings.cooldown_seconds)
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            now = time.time()

            # Push frame to VPS for the dashboard live preview
            if now - last_frame_push >= frame_push_interval:
                jpeg = _encode_jpeg(frame)
                if jpeg:
                    api.push_frame(jpeg)
                last_frame_push = now

            # Run recognition
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            employee_id, confidence = recognizer.match(rgb)

            if employee_id:
                if employee_id == last_employee and (now - last_match_at) < settings.cooldown_seconds:
                    continue  # debounce
                last_match_at = now
                last_employee = employee_id
                logger.info("Match: %s confidence=%.2f", employee_id, confidence or 0.0)
                api.report_face_match(
                    is_valid=True,
                    employee_id=employee_id,
                    confidence=confidence,
                    direction="in",
                )
            else:
                # only report invalid if we actually saw a face we couldn't match
                if confidence is not None:
                    if (now - last_match_at) >= settings.cooldown_seconds:
                        last_match_at = now
                        last_employee = None
                        logger.info("Unknown face confidence=%.2f", confidence)
                        api.report_face_match(
                            is_valid=False,
                            employee_id=None,
                            confidence=confidence,
                            message="unknown_face",
                            direction="in",
                        )
    except KeyboardInterrupt:
        logger.info("Shutting down…")
    finally:
        stop.set()
        cap.release()
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
