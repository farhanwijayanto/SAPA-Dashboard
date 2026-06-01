"""SAPA Edge Server — entry point.

Jalankan:  python -m edge_server.main

Yang dilakukan service ini di laptop dekat gate:
  1. Buka webcam (OpenCV VideoCapture)
  2. Sync foto karyawan dari VPS tiap SYNC_EVERY_SECONDS (otomatis pickup
     foto baru yang diupload manager via dashboard)
  3. Face recognition lokal pakai pretrained face_recognition
  4. Push frame ke VPS /edge/frame -> tampil di Dashboard System tab Live Camera
  5. Saat match: POST /edge/face-match -> backend buka gate (MQTT) + log
     + POST /edge/events -> halaman /edge tampil PRESENSI BERHASIL/GAGAL
  6. Window lokal "SAPA Edge" dengan kotak HIJAU (match) / MERAH (unknown)
     (window di-skip otomatis kalau jalan headless / tanpa display)

Env penting (lihat .env.example):
  SAPA_API_BASE=https://sapa.farhn.dev/api   <-- WAJIB pakai /api
  EDGE_INGEST_KEY=...                         <-- dari secret VPS
  SAPA_USERNAME / SAPA_PASSWORD               <-- untuk auto-login (sync foto)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import webbrowser
from typing import Dict, Optional

try:
    import paho.mqtt.client as mqtt
    _HAS_MQTT = True
except Exception:  # pragma: no cover
    mqtt = None  # type: ignore
    _HAS_MQTT = False

try:
    import cv2  # type: ignore
    _HAS_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    _HAS_CV2 = False

from .config import load_settings
from .liveness import LivenessDetector
from .overlay import (
    FaceAnnotation,
    draw_face_box,
    draw_help_footer,
    draw_status_banner,
)
from .recognizer import FaceRecognizer
from .sapa_client import SapaClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger("sapa.edge")


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_match_id: Optional[str] = None
        self.synced_count: int = 0
        self.mqtt_connected: bool = False
        self.employee_names: Dict[str, str] = {}


# ---------------------------------------------------------------------------
# MQTT (opsional — heartbeat supaya dashboard tahu gate online)
# ---------------------------------------------------------------------------

def _start_mqtt(settings, state: SharedState):
    if not _HAS_MQTT:
        logger.info("paho-mqtt tidak terinstall; MQTT di-skip")
        return None
    client = mqtt.Client(client_id=f"sapa-edge-{int(time.time())}")
    if settings.mqtt_username:
        client.username_pw_set(settings.mqtt_username, settings.mqtt_password or "")
    client.will_set(
        settings.mqtt_topic_heartbeat,
        json.dumps({"online": False, "source": "edge"}),
        qos=1, retain=False,
    )

    def on_connect(_c, _u, _f, rc):
        logger.info("MQTT connected rc=%s", rc)
        with state.lock:
            state.mqtt_connected = (rc == 0)

    def on_disconnect(_c, _u, rc):
        logger.warning("MQTT disconnected rc=%s", rc)
        with state.lock:
            state.mqtt_connected = False

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    try:
        client.connect(settings.mqtt_broker, settings.mqtt_port, 60)
        client.loop_start()
    except Exception as exc:
        logger.warning("MQTT connect failed: %s", exc)
    return client


def _heartbeat_loop(client, topic: str, stop: threading.Event):
    while not stop.is_set():
        if client is not None:
            try:
                client.publish(
                    topic,
                    json.dumps({"online": True, "source": "edge", "ts": time.time()}),
                    qos=0,
                )
            except Exception:
                pass
        stop.wait(5)


# ---------------------------------------------------------------------------
# Sync foto dari VPS
# ---------------------------------------------------------------------------

def _do_sync(api: SapaClient, recognizer: FaceRecognizer, state: SharedState) -> None:
    try:
        api.ensure_login()
        faces = api.list_faces()

        # Map id -> nama untuk overlay
        names: Dict[str, str] = {}
        for f in faces:
            if f.get("name"):
                names[str(f.get("employee_id"))] = f["name"]
        if not names:
            for emp in api.list_employees():
                eid = str(emp.get("id", "")).strip()
                nm = (emp.get("name") or "").strip()
                if eid and nm:
                    names[eid] = nm

        samples = []
        for f in faces:
            emp_id = str(f.get("employee_id", "")).strip()
            url = f.get("url", "")
            if not emp_id or not url:
                continue
            blob = api.fetch_face_image(url)
            if blob:
                samples.append((emp_id, blob))

        if samples:
            count = recognizer.rebuild(samples)
            with state.lock:
                state.synced_count = count
                state.employee_names = names
            logger.info("Synced %d faces -> %d embeddings", len(samples), count)
        else:
            with state.lock:
                state.synced_count = recognizer.known_count
                state.employee_names = names
            logger.info("Tidak ada foto untuk di-sync (cache=%d)", recognizer.known_count)
    except Exception as exc:
        logger.warning("Sync failed: %s", exc)


def _sync_loop(api, recognizer, every, stop, state, trigger):
    while not stop.is_set():
        _do_sync(api, recognizer, state)
        if trigger.wait(timeout=every):
            trigger.clear()


# ---------------------------------------------------------------------------
# Frame helper
# ---------------------------------------------------------------------------

def _encode_jpeg(frame_bgr, quality: int = 70):
    if not _HAS_CV2:
        return None
    ok, buf = cv2.imencode(".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return None
    return buf.tobytes()


def _has_display() -> bool:
    """Cek apakah ada display untuk cv2.imshow (window lokal)."""
    if not _HAS_CV2:
        return False
    # Linux tanpa DISPLAY -> headless
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    if os.environ.get("SAPA_HEADLESS", "").strip() in ("1", "true", "True"):
        return False
    return True


def _open_edge_page(url: str) -> None:
    """Buka halaman /edge di browser default (sekali saat start).

    Dijalankan di thread terpisah + delay kecil supaya tidak mengganggu
    startup kamera. Aman di Windows/macOS/Linux desktop; di server headless
    webbrowser.open() akan no-op tanpa error.
    """
    def _worker():
        time.sleep(2.0)
        try:
            opened = webbrowser.open(url, new=2)
            if opened:
                logger.info("Browser dibuka ke halaman edge: %s", url)
            else:
                logger.info("Tidak bisa buka browser otomatis. Buka manual: %s", url)
        except Exception as exc:
            logger.info("Auto-open browser gagal (%s). Buka manual: %s", exc, url)

    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    settings = load_settings()

    if not _HAS_CV2:
        logger.error("OpenCV (cv2) tidak terinstall. pip install -r edge_server/requirements.txt")
        return 1

    logger.info("SAPA_API_BASE = %s", settings.api_base)
    logger.info("MQTT broker   = %s:%s user=%s", settings.mqtt_broker, settings.mqtt_port, settings.mqtt_username or "(none)")
    if not settings.edge_ingest_key:
        logger.warning("EDGE_INGEST_KEY kosong! Frame push & face-match kemungkinan 401.")

    recognizer = FaceRecognizer(threshold=settings.recognition_threshold)
    recognizer.load_cache()

    # Liveness anti-spoof (deteksi wajah asli vs foto/layar HP)
    liveness = None
    if settings.liveness_enabled:
        liveness = LivenessDetector(
            threshold=settings.liveness_threshold,
            use_blink=True,
            require_blink=settings.liveness_require_blink,
        )
        logger.info(
            "Liveness AKTIF (threshold=%.2f require_blink=%s)",
            settings.liveness_threshold, settings.liveness_require_blink,
        )
    else:
        logger.info("Liveness NONAKTIF (LIVENESS_ENABLED=false)")

    api = SapaClient(settings)
    state = SharedState()
    state.synced_count = recognizer.known_count

    # Auto-buka halaman /edge di browser (pengenalan wajah versi browser)
    if settings.open_browser:
        _open_edge_page(settings.edge_page_url)

    mqtt_client = _start_mqtt(settings, state)

    stop_evt = threading.Event()
    sync_trigger = threading.Event()
    threading.Thread(
        target=_heartbeat_loop,
        args=(mqtt_client, settings.mqtt_topic_heartbeat, stop_evt),
        daemon=True,
    ).start()
    threading.Thread(
        target=_sync_loop,
        args=(api, recognizer, settings.sync_every_seconds, stop_evt, state, sync_trigger),
        daemon=True,
    ).start()

    cap = cv2.VideoCapture(settings.camera_index)
    if not cap.isOpened():
        logger.error("Gagal buka camera index %s", settings.camera_index)
        return 2
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    frame_push_interval = 1.0 / max(1, settings.frame_push_fps)
    last_frame_push = 0.0
    last_match_at = 0.0
    last_employee: Optional[str] = None
    first_frame_push_logged = False

    fps_t0 = time.time()
    fps_n = 0
    fps_value = 0.0

    use_window = _has_display()
    if use_window:
        cv2.namedWindow("SAPA Edge", cv2.WINDOW_AUTOSIZE)
        logger.info("Window 'SAPA Edge' aktif. [Q] keluar, [R] resync")
    else:
        logger.info("Headless mode (tanpa window). Frame tetap dikirim ke dashboard.")

    try:
        while True:
            ok, frame_bgr = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            now = time.time()

            # 1) Push frame ke VPS -> dashboard Live Camera
            if now - last_frame_push >= frame_push_interval:
                jpeg = _encode_jpeg(frame_bgr)
                if jpeg:
                    success, code = api.push_frame(jpeg)
                    if not first_frame_push_logged:
                        if success:
                            logger.info("Frame push OK -> dashboard Live Camera aktif")
                        else:
                            logger.warning("Frame push gagal (HTTP %s). Cek EDGE_INGEST_KEY / SAPA_API_BASE.", code)
                        first_frame_push_logged = True
                last_frame_push = now

            # 2) Recognition
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            matches = recognizer.detect_and_match(rgb)

            with state.lock:
                names = state.employee_names.copy()
                synced = state.synced_count
                mqtt_ok = state.mqtt_connected
                last_id = state.last_match_id

            # 3) Gambar kotak
            for m in matches:
                draw_face_box(frame_bgr, FaceAnnotation(
                    bbox=m.bbox,
                    employee_id=m.employee_id,
                    display_name=names.get(m.employee_id) if m.employee_id else None,
                    confidence=m.confidence,
                    is_valid=m.is_valid,
                ))
            draw_status_banner(
                frame_bgr,
                fps=fps_value,
                synced_count=synced,
                last_match_id=last_id,
                mqtt_connected=mqtt_ok,
            )
            draw_help_footer(frame_bgr)

            if use_window:
                cv2.imshow("SAPA Edge", frame_bgr)

            # 4) Pilih best match + lapor ke VPS (debounce)
            best_valid = None
            best_unknown = None
            for m in matches:
                area = (m.bbox[2] - m.bbox[0]) * (m.bbox[1] - m.bbox[3])
                if m.is_valid and m.employee_id:
                    if best_valid is None or area > best_valid[1]:
                        best_valid = (m, area)
                else:
                    if best_unknown is None or area > best_unknown[1]:
                        best_unknown = (m, area)

            if best_valid is not None:
                m = best_valid[0]
                # --- LIVENESS CHECK: pastikan wajah ASLI, bukan foto/layar HP ---
                live_ok = True
                live_reason = ""
                if liveness is not None:
                    lres = liveness.analyze(frame_bgr, m.bbox)
                    live_ok = lres.is_live
                    live_reason = lres.reason
                    # Gambar status liveness di overlay (kuning kalau spoof)
                    if not live_ok:
                        try:
                            top, right, bottom, left = m.bbox
                            cv2.putText(
                                frame_bgr, "SPOOF?", (left, bottom + 22),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 220), 2, cv2.LINE_AA,
                            )
                        except Exception:
                            pass

                if not live_ok:
                    # Wajah dikenali TAPI terindikasi spoof -> JANGAN buka gate
                    if (now - last_match_at) >= settings.cooldown_seconds:
                        last_match_at = now
                        last_employee = None
                        logger.warning(
                            "SPOOF terdeteksi untuk %s -> gate TIDAK dibuka (%s)",
                            m.employee_id, live_reason,
                        )
                        api.report_face_match(
                            is_valid=False, employee_id=m.employee_id,
                            confidence=m.confidence, message="liveness_failed",
                        )
                        api.push_edge_event(
                            is_valid=False, employee_id=m.employee_id,
                            message="Wajah terdeteksi palsu (foto/layar)",
                        )
                        with state.lock:
                            state.last_match_id = "spoof"
                elif m.employee_id == last_employee and (now - last_match_at) < settings.cooldown_seconds:
                    pass
                else:
                    last_match_at = now
                    last_employee = m.employee_id
                    logger.info("MATCH: %s confidence=%.2f live=%s",
                                m.employee_id, m.confidence, live_reason or "off")
                    success, resp = api.report_face_match(
                        is_valid=True, employee_id=m.employee_id,
                        confidence=m.confidence, direction="in",
                    )
                    # Update banner halaman /edge
                    api.push_edge_event(
                        is_valid=True, employee_id=m.employee_id,
                        message=f"Welcome {names.get(m.employee_id, m.employee_id)}",
                    )
                    if success:
                        with state.lock:
                            state.last_match_id = m.employee_id
                        logger.info("  -> VPS: %s", resp)
            elif best_unknown is not None:
                if (now - last_match_at) >= settings.cooldown_seconds:
                    last_match_at = now
                    last_employee = None
                    logger.info("UNKNOWN confidence=%.2f", best_unknown[0].confidence)
                    api.report_face_match(
                        is_valid=False, employee_id=None,
                        confidence=best_unknown[0].confidence, message="unknown_face",
                    )
                    api.push_edge_event(is_valid=False, employee_id=None, message="Wajah tidak dikenali")
                    with state.lock:
                        state.last_match_id = "unknown"
            else:
                # Tidak ada wajah -> reset state blink liveness
                if liveness is not None:
                    liveness.reset_blink()

            # FPS
            fps_n += 1
            if now - fps_t0 >= 1.0:
                fps_value = fps_n / (now - fps_t0)
                fps_n = 0
                fps_t0 = now

            # Keyboard (hanya kalau ada window)
            if use_window:
                key = cv2.waitKey(1) & 0xFF
                if key in (ord('q'), 27):
                    break
                elif key == ord('r'):
                    logger.info("Manual resync")
                    sync_trigger.set()
            else:
                # Headless: throttle CPU sedikit
                time.sleep(0.01)

    except KeyboardInterrupt:
        logger.info("Shutting down (Ctrl+C)")
    finally:
        stop_evt.set()
        cap.release()
        if use_window:
            cv2.destroyAllWindows()
        if mqtt_client is not None:
            try:
                mqtt_client.loop_stop()
                mqtt_client.disconnect()
            except Exception:
                pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
