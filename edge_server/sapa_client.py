"""Thin HTTP client for the SAPA VPS backend.

The edge server uses this client to:
- pull employee records and reference face images (so the AI can match locally)
- push live frames to the VPS for the dashboard live preview
- report face recognition results, which the backend turns into MongoDB logs + MQTT gate commands
"""
from __future__ import annotations

import io
import logging
from typing import Optional

import requests

from .config import Settings

logger = logging.getLogger("sapa.edge.client")


class SapaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session = requests.Session()
        if settings.api_token:
            self._session.headers["Authorization"] = f"Bearer {settings.api_token}"

    def _url(self, path: str) -> str:
        if path.startswith("/"):
            return f"{self.settings.api_base}{path}"
        return f"{self.settings.api_base}/{path}"

    def list_employees(self) -> list[dict]:
        r = self._session.get(self._url("/employees/"), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_face_image(self, employee_id: str) -> Optional[bytes]:
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            url = self._url(f"/uploads/faces/{employee_id}{ext}")
            try:
                r = self._session.get(url, timeout=10)
                if r.status_code == 200 and r.content:
                    return r.content
            except Exception:
                continue
        return None

    def push_frame(self, jpeg_bytes: bytes) -> bool:
        try:
            files = {"frame": ("frame.jpg", io.BytesIO(jpeg_bytes), "image/jpeg")}
            headers = {}
            if self.settings.edge_ingest_key:
                headers["X-EDGE-KEY"] = self.settings.edge_ingest_key
            r = self._session.post(self._url("/edge/frame"), files=files, headers=headers, timeout=10)
            return r.status_code in (200, 201)
        except Exception as exc:
            logger.warning("push_frame failed: %s", exc)
            return False

    def report_face_match(
        self,
        is_valid: bool,
        employee_id: Optional[str],
        confidence: Optional[float] = None,
        message: Optional[str] = None,
        direction: str = "in",
    ) -> bool:
        payload: dict = {
            "is_valid": is_valid,
            "direction": direction,
        }
        if employee_id is not None:
            payload["employee_id"] = str(employee_id)
        if confidence is not None:
            payload["confidence"] = float(confidence)
        if message:
            payload["message"] = message
        if self.settings.edge_ingest_key:
            payload["edge_key"] = self.settings.edge_ingest_key
        try:
            r = self._session.post(self._url("/edge/face-match"), json=payload, timeout=10)
            return r.status_code in (200, 201)
        except Exception as exc:
            logger.warning("report_face_match failed: %s", exc)
            return False

    def push_edge_event(self, is_valid: bool, employee_id: Optional[str], message: Optional[str]) -> bool:
        payload = {"is_valid": is_valid}
        if employee_id is not None:
            payload["employee_id"] = str(employee_id)
        if message:
            payload["message"] = message
        try:
            r = self._session.post(self._url("/edge/events"), json=payload, timeout=10)
            return r.status_code in (200, 201)
        except Exception as exc:
            logger.warning("push_edge_event failed: %s", exc)
            return False
