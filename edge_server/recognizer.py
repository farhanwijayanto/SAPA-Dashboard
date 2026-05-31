"""Face recognition wrapper untuk edge server.

Pakai library ``face_recognition`` (dlib ResNet-34 pretrained, 99.38% LFW).
Cache 128-d embedding per karyawan di ``embeddings.npz`` supaya inference
tidak perlu re-encode foto referensi setiap frame.

Library `face_recognition` melakukan semua AI work — kita tinggal panggil
3 function:
  - face_recognition.face_locations(rgb_frame, model="hog")
  - face_recognition.face_encodings(rgb_frame, locations)
  - np.linalg.norm() untuk Euclidean distance match

Tidak ada training di edge: model sudah pretrained, kita hanya
"encode + nearest neighbor search".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger("sapa.edge.recognizer")

try:
    import face_recognition  # type: ignore
    _HAS_FACE_RECOGNITION = True
except Exception:  # pragma: no cover
    _HAS_FACE_RECOGNITION = False
    face_recognition = None  # type: ignore

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore


CACHE_PATH = Path(__file__).resolve().parent / "embeddings.npz"


@dataclass
class FaceMatch:
    """Hasil deteksi+match satu wajah dalam satu frame."""

    # bbox dalam koordinat frame (top, right, bottom, left)
    bbox: Tuple[int, int, int, int]
    # employee_id kalau match, None kalau tidak match
    employee_id: Optional[str]
    # confidence 0..1 (1 = identik)
    confidence: float
    # True kalau distance < threshold
    is_valid: bool


class FaceRecognizer:
    """Encode + match wajah di frame.

    Konstruktor side-effect free (tidak load cache atau buka file).
    Panggil ``load_cache()`` setelah __init__ kalau mau pakai cache lama,
    atau ``rebuild()`` setelah download foto baru dari VPS.
    """

    def __init__(self, threshold: float = 0.55):
        self.threshold = threshold
        self._labels: List[str] = []
        self._embeddings: np.ndarray = np.zeros((0, 128), dtype=np.float32)

    @property
    def known_count(self) -> int:
        return self._embeddings.shape[0]

    def is_ready(self) -> bool:
        return self.known_count > 0 and _HAS_FACE_RECOGNITION

    def load_cache(self) -> None:
        """Load embeddings.npz dari disk kalau ada."""
        if not CACHE_PATH.exists():
            return
        try:
            data = np.load(CACHE_PATH, allow_pickle=False)
            labels = data.get("labels")
            embs = data.get("embeddings")
            if labels is not None and embs is not None and len(labels) == embs.shape[0]:
                self._labels = [str(x) for x in labels.tolist()]
                self._embeddings = embs.astype(np.float32)
                logger.info("Loaded %d cached embeddings", self.known_count)
        except Exception as exc:
            logger.warning("Failed to load cache: %s", exc)

    def save_cache(self) -> None:
        """Simpan embeddings ke disk."""
        try:
            np.savez(
                CACHE_PATH,
                labels=np.array(self._labels),
                embeddings=self._embeddings,
            )
        except Exception as exc:
            logger.warning("Failed to save cache: %s", exc)

    def rebuild(self, samples: List[Tuple[str, bytes]]) -> int:
        """Rebuild embeddings dari list (employee_id, image_bytes).

        Dipanggil tiap sync (60 detik) oleh main loop. Kalau
        `face_recognition.face_encodings()` tidak mendeteksi wajah di
        salah satu foto, foto itu di-skip dengan log peringatan.
        """
        if not _HAS_FACE_RECOGNITION:
            logger.warning("face_recognition not installed; rebuild() is a no-op")
            return 0

        labels: List[str] = []
        embs: List[np.ndarray] = []

        for emp_id, blob in samples:
            try:
                arr = np.frombuffer(blob, dtype=np.uint8)
                if cv2 is None:
                    continue
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is None:
                    logger.warning("Failed to decode image for %s", emp_id)
                    continue
                rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                # AI INFERENCE: ini yang panggil pretrained ResNet-34
                # untuk dapatkan 128-d embedding. Tidak ada training.
                encs = face_recognition.face_encodings(rgb)
                if not encs:
                    logger.info("No face found in photo for %s, skipping", emp_id)
                    continue
                labels.append(emp_id)
                embs.append(encs[0].astype(np.float32))
            except Exception as exc:
                logger.warning("Failed to encode %s: %s", emp_id, exc)

        if embs:
            self._labels = labels
            self._embeddings = np.vstack(embs)
            self.save_cache()
        return self.known_count

    def detect_and_match(self, frame_rgb: np.ndarray) -> List[FaceMatch]:
        """Deteksi semua wajah di frame, match ke cache.

        Return list FaceMatch — bisa kosong kalau tidak ada wajah,
        atau berisi banyak wajah kalau frame multiwajah. Setiap face
        punya bbox + label match (atau None kalau unknown).

        Frame harus dalam format RGB (output cv2.cvtColor BGR2RGB).
        """
        if not _HAS_FACE_RECOGNITION or frame_rgb is None:
            return []

        try:
            # Step 1: Cari kotak wajah pakai HOG (cepat, CPU-friendly)
            locations = face_recognition.face_locations(frame_rgb, model="hog")
            if not locations:
                return []

            # Step 2: Encode tiap wajah jadi 128-d vector
            encs = face_recognition.face_encodings(frame_rgb, locations)
            if not encs:
                return []
        except Exception as exc:
            logger.warning("Face encode failed: %s", exc)
            return []

        results: List[FaceMatch] = []
        for loc, enc in zip(locations, encs):
            top, right, bottom, left = loc
            bbox = (int(top), int(right), int(bottom), int(left))

            if self.known_count == 0:
                # Tidak ada cache — semua wajah dianggap unknown
                results.append(FaceMatch(
                    bbox=bbox,
                    employee_id=None,
                    confidence=0.0,
                    is_valid=False,
                ))
                continue

            # Step 3: Hitung jarak Euclidean ke semua embedding di cache
            enc_f32 = enc.astype(np.float32)
            distances = np.linalg.norm(self._embeddings - enc_f32, axis=1)
            best_idx = int(np.argmin(distances))
            best_dist = float(distances[best_idx])
            confidence = max(0.0, 1.0 - best_dist)

            if best_dist <= self.threshold:
                results.append(FaceMatch(
                    bbox=bbox,
                    employee_id=self._labels[best_idx],
                    confidence=confidence,
                    is_valid=True,
                ))
            else:
                results.append(FaceMatch(
                    bbox=bbox,
                    employee_id=None,
                    confidence=confidence,
                    is_valid=False,
                ))

        return results

    # Backward-compat: kode lama panggil .match() yang return single best.
    def match(self, frame_rgb: np.ndarray) -> Tuple[Optional[str], Optional[float]]:
        """Single-match versi kompatibilitas — return wajah terbesar."""
        matches = self.detect_and_match(frame_rgb)
        if not matches:
            return None, None
        # Pilih wajah dengan area terbesar (kemungkinan paling dekat ke kamera)
        best = max(
            matches,
            key=lambda m: (m.bbox[2] - m.bbox[0]) * (m.bbox[1] - m.bbox[3]),
        )
        return best.employee_id, best.confidence
