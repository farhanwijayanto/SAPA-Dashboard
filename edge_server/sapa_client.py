"""HTTP client ke VPS backend SAPA.

Client ini sengaja dibuat ROBUST terhadap dua varian backend:
  1. Backend dengan modul AI Gate (endpoint /api/edge/faces + /api/static/faces)
  2. Backend lama tanpa AI Gate (fallback ke /employees/ + /employees/{id}/faces)

Endpoint yang dipakai (relatif terhadap api_base, mis. https://sapa.farhn.dev/api):
  POST /login                      -> dapat JWT (auto-login)
  GET  /employees/                 -> daftar karyawan (butuh JWT)
  GET  /edge/faces                 -> [AI Gate] daftar foto (X-EDGE-KEY)   *opsional*
  GET  /employees/{id}/faces       -> [fallback] foto per karyawan (JWT)
  GET  /uploads/faces/{file}       -> download foto referensi
  POST /edge/frame                 -> push frame kamera (live preview dashboard)
  POST /edge/face-match            -> lapor hasil match (gate + log)
  GET  /edge/status                -> status terakhir (untuk debug)
"""
from __future__ import annotations

import io
import logging
from typing import Dict, List, Optional, Tuple

import requests

from .config import Settings

logger = logging.getLogger("sapa.edge.client")


class SapaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session = requests.Session()
        self._jwt: Optional[str] = settings.api_token or None
        if self._jwt:
            self._session.headers["Authorization"] = f"Bearer {self._jwt}"

    # ------------------------------------------------------------------
    # URL helper
    # ------------------------------------------------------------------
    def _url(self, path: str) -> str:
        base = self.settings.api_base.rstrip("/")
        if not path.startswith("/"):
            path = "/" + path
        return base + path

    def _origin(self) -> str:
        """Origin tanpa /api (untuk static files seperti /uploads/...)."""
        base = self.settings.api_base.rstrip("/")
        if base.endswith("/api"):
            return base[:-4]
        return base

    # ------------------------------------------------------------------
    # Auto-login: dapat JWT supaya bisa GET /employees/
    # ------------------------------------------------------------------
    def ensure_login(self) -> bool:
        """Login pakai SAPA_USERNAME/SAPA_PASSWORD kalau JWT belum ada.

        Backend SAPA login menerima JSON {username, password} di POST /login.
        JWT disimpan di session header untuk request berikutnya.
        """
        if self._jwt:
            return True
        if not self.settings.sapa_password:
            logger.info("SAPA_PASSWORD kosong; skip auto-login (pakai X-EDGE-KEY saja)")
            return False
        try:
            r = self._session.post(
                self._url("/login"),
                json={
                    "username": self.settings.sapa_username,
                    "password": self.settings.sapa_password,
                },
                timeout=10,
            )
            if r.status_code == 200:
                token = r.json().get("access_token")
                if token:
                    self._jwt = token
                    self._session.headers["Authorization"] = f"Bearer {token}"
                    logger.info("Auto-login OK as %s", self.settings.sapa_username)
                    return True
            logger.warning("Auto-login gagal -> %s: %s", r.status_code, r.text[:160])
        except Exception as exc:
            logger.warning("Auto-login error: %s", exc)
        return False

    def _edge_headers(self) -> Dict[str, str]:
        h: Dict[str, str] = {}
        if self.settings.edge_ingest_key:
            h["X-EDGE-KEY"] = self.settings.edge_ingest_key
        return h

    # ------------------------------------------------------------------
    # Sync foto karyawan — coba AI Gate dulu, fallback ke /employees/
    # ------------------------------------------------------------------
    def list_employees(self) -> List[Dict]:
        """GET /employees/ (butuh JWT)."""
        try:
            r = self._session.get(self._url("/employees/"), timeout=10)
            if r.status_code == 200:
                return r.json() or []
            logger.warning("list_employees -> %s", r.status_code)
        except Exception as exc:
            logger.warning("list_employees failed: %s", exc)
        return []

    def list_faces(self) -> List[Dict]:
        """Return [{"employee_id", "url", "name"?}, ...].

        Strategi:
          1. Coba endpoint AI Gate GET /edge/faces (X-EDGE-KEY).
          2. Kalau gagal/404, fallback: GET /employees/ lalu untuk tiap
             karyawan ambil /employees/{id}/faces.
        """
        # --- Coba AI Gate ---
        try:
            r = self._session.get(
                self._url("/edge/faces"),
                headers=self._edge_headers(),
                timeout=10,
            )
            if r.status_code == 200:
                faces = r.json().get("faces", []) or []
                if faces:
                    logger.info("list_faces via AI Gate /edge/faces: %d", len(faces))
                    return faces
                # 200 tapi kosong -> tetap pakai (mungkin belum ada foto)
                logger.info("AI Gate /edge/faces kosong, coba fallback /employees/")
            else:
                logger.info("AI Gate /edge/faces -> %s, fallback ke /employees/", r.status_code)
        except Exception as exc:
            logger.info("AI Gate /edge/faces error (%s); fallback ke /employees/", exc)

        # --- Fallback: legacy /employees/ + /employees/{id}/faces ---
        self.ensure_login()
        employees = self.list_employees()
        faces: List[Dict] = []
        for emp in employees:
            emp_id = str(emp.get("id", "")).strip()
            if not emp_id:
                continue
            try:
                rr = self._session.get(self._url(f"/employees/{emp_id}/faces"), timeout=10)
                if rr.status_code == 200:
                    for f in rr.json().get("faces", []) or []:
                        faces.append({
                            "employee_id": emp_id,
                            "url": f.get("url"),
                            "name": emp.get("name"),
                        })
            except Exception:
                continue
        logger.info("list_faces via fallback /employees/: %d", len(faces))
        return faces

    def fetch_face_image(self, url_path: str) -> Optional[bytes]:
        """Download foto dari url_path (mis. /uploads/faces/513061.jpg
        atau /api/static/faces/513061.jpg)."""
        if not url_path:
            return None
        # url_path bisa absolut (http...) atau relatif (/uploads/... atau /api/static/...)
        if url_path.startswith("http"):
            full = url_path
        else:
            full = self._origin() + url_path
        try:
            r = self._session.get(full, headers=self._edge_headers(), timeout=10)
            if r.status_code == 200 and r.content:
                return r.content
            logger.warning("fetch_face_image %s -> %s", full, r.status_code)
        except Exception as exc:
            logger.warning("fetch_face_image failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Live frame push -> dashboard System tab Live Camera (/edge/frame.jpg)
    # ------------------------------------------------------------------
    def push_frame(self, jpeg_bytes: bytes) -> Tuple[bool, Optional[int]]:
        """POST /edge/frame. Auth: X-EDGE-KEY atau JWT.

        Return (success, http_status). http_status berguna untuk debug 401/413.
        """
        try:
            files = {"frame": ("frame.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")}
            r = self._session.post(
                self._url("/edge/frame"),
                files=files,
                headers=self._edge_headers(),
                timeout=10,
            )
            return (r.status_code in (200, 201), r.status_code)
        except Exception as exc:
            logger.debug("push_frame failed: %s", exc)
            return (False, None)

    # ------------------------------------------------------------------
    # Face match report -> gate command + attendance log
    # ------------------------------------------------------------------
    def report_face_match(
        self,
        is_valid: bool,
        employee_id: Optional[str],
        confidence: Optional[float] = None,
        message: Optional[str] = None,
        direction: str = "in",
    ) -> Tuple[bool, Optional[Dict]]:
        """POST /edge/face-match.

        Backend legacy menerima edge_key DI DALAM body (schema FaceMatchEvent),
        sedangkan AI Gate menerima X-EDGE-KEY di header. Kita kirim KEDUANYA
        supaya kompatibel dengan dua-duanya.
        """
        payload: Dict = {"is_valid": is_valid, "direction": direction}
        if employee_id is not None:
            payload["employee_id"] = str(employee_id)
        if confidence is not None:
            payload["confidence"] = float(confidence)
        if message:
            payload["message"] = message
        # Legacy: edge_key di body
        if self.settings.edge_ingest_key:
            payload["edge_key"] = self.settings.edge_ingest_key

        try:
            r = self._session.post(
                self._url("/edge/face-match"),
                json=payload,
                headers=self._edge_headers(),  # AI Gate: X-EDGE-KEY di header
                timeout=10,
            )
            if r.status_code == 200:
                try:
                    return True, r.json()
                except Exception:
                    return True, None
            logger.warning("report_face_match -> %s: %s", r.status_code, r.text[:200])
        except Exception as exc:
            logger.warning("report_face_match failed: %s", exc)
        return False, None

    def push_edge_event(
        self,
        is_valid: bool,
        employee_id: Optional[str] = None,
        message: Optional[str] = None,
    ) -> bool:
        """POST /edge/events — update status banner di halaman /edge.

        Berguna supaya halaman /edge (browser) menampilkan PRESENSI BERHASIL/
        GAGAL walau frame dikirim dari edge server Python.
        """
        payload: Dict = {"is_valid": is_valid}
        if employee_id is not None:
            payload["employee_id"] = str(employee_id)
        if message:
            payload["message"] = message
        try:
            r = self._session.post(self._url("/edge/events"), json=payload, timeout=10)
            return r.status_code in (200, 201)
        except Exception as exc:
            logger.debug("push_edge_event failed: %s", exc)
            return False

    def get_status(self) -> Optional[Dict]:
        """GET /edge/status — untuk debug."""
        try:
            r = self._session.get(self._url("/edge/status"), timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None
