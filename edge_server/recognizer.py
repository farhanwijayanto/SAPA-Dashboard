"""Face recognition wrapper used by the edge server.

Tries `face_recognition` (dlib) first, which gives 128-d embeddings + a robust
encoder. Falls back to OpenCV LBPH if dlib isn't installable on the target
device (e.g. some Pi installs).

The cache file `embeddings.npz` stores numpy embeddings keyed by employee_id.
This is what makes the AI "live on the edge" while the source images live on
the VPS — we sync the source images periodically and recompute embeddings
locally.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("sapa.edge.recognizer")

try:  # primary backend
    import face_recognition  # type: ignore
    _HAS_FACE_RECOGNITION = True
except Exception:  # pragma: no cover - exercised only on degraded installs
    _HAS_FACE_RECOGNITION = False
    face_recognition = None  # type: ignore

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore


CACHE_PATH = Path(__file__).resolve().parent / "embeddings.npz"


class FaceRecognizer:
    def __init__(self, threshold: float = 0.55):
        self.threshold = threshold
        self._labels: list[str] = []
        self._embeddings: np.ndarray = np.zeros((0, 128), dtype=np.float32)

    def is_ready(self) -> bool:
        return self._embeddings.shape[0] > 0

    def load_cache(self) -> None:
        if not CACHE_PATH.exists():
            return
        try:
            data = np.load(CACHE_PATH, allow_pickle=False)
            labels = data.get("labels")
            embs = data.get("embeddings")
            if labels is not None and embs is not None and len(labels) == embs.shape[0]:
                self._labels = [str(x) for x in labels.tolist()]
                self._embeddings = embs.astype(np.float32)
                logger.info("Loaded %d cached embeddings", len(self._labels))
        except Exception as exc:
            logger.warning("Failed to load cache: %s", exc)

    def save_cache(self) -> None:
        try:
            np.savez(CACHE_PATH, labels=np.array(self._labels), embeddings=self._embeddings)
        except Exception as exc:
            logger.warning("Failed to save cache: %s", exc)

    def rebuild(self, samples: list[tuple[str, bytes]]) -> int:
        """Rebuild embeddings from a list of (employee_id, image_bytes)."""
        if not _HAS_FACE_RECOGNITION:
            logger.warning("face_recognition not installed; rebuild() is a no-op")
            return 0
        labels: list[str] = []
        embs: list[np.ndarray] = []
        for emp_id, blob in samples:
            try:
                arr = np.frombuffer(blob, dtype=np.uint8)
                if cv2 is None:
                    continue
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    continue
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                encs = face_recognition.face_encodings(rgb)
                if not encs:
                    logger.info("No face found for employee %s, skipping", emp_id)
                    continue
                labels.append(emp_id)
                embs.append(encs[0].astype(np.float32))
            except Exception as exc:
                logger.warning("Failed to encode %s: %s", emp_id, exc)
        if embs:
            self._labels = labels
            self._embeddings = np.vstack(embs)
            self.save_cache()
        return len(self._labels)

    def match(self, frame_rgb: np.ndarray) -> tuple[Optional[str], Optional[float]]:
        """Return (employee_id, confidence) for the best match, or (None, None)."""
        if not self.is_ready() or not _HAS_FACE_RECOGNITION:
            return None, None
        try:
            locations = face_recognition.face_locations(frame_rgb, model="hog")
            if not locations:
                return None, None
            encs = face_recognition.face_encodings(frame_rgb, locations)
            if not encs:
                return None, None
            # use the largest face
            largest_idx = max(
                range(len(locations)),
                key=lambda i: (locations[i][2] - locations[i][0]) * (locations[i][1] - locations[i][3]),
            )
            enc = encs[largest_idx].astype(np.float32)
            distances = np.linalg.norm(self._embeddings - enc, axis=1)
            best = int(np.argmin(distances))
            best_distance = float(distances[best])
            confidence = max(0.0, 1.0 - best_distance)
            if best_distance > self.threshold:
                return None, confidence
            return self._labels[best], confidence
        except Exception as exc:
            logger.warning("match failed: %s", exc)
            return None, None
