"""Liveness detection untuk edge server SAPA.

Tujuan: membedakan WAJAH ASLI (orang sungguhan di depan kamera) dari
SPOOF/kecurangan (foto wajah di HP, layar laptop, cetakan foto, dll).

Pendekatan: PASSIVE LIVENESS multi-cue (tidak butuh model ML tambahan,
hanya OpenCV + numpy + landmark dari face_recognition yang sudah ada).
Empat cue digabung jadi satu skor 0..1:

  1. MOIRE / FREQUENCY ANALYSIS (bobot terbesar)
     Layar HP/laptop punya struktur piksel + refresh yang menghasilkan
     energi frekuensi-tinggi periodik (pola moiré) saat difoto ulang.
     Wajah asli punya distribusi frekuensi lebih halus/alami.
     Diukur lewat FFT pada channel grayscale crop wajah.

  2. TEXTURE / SHARPNESS (Laplacian variance)
     Layar/foto cetak sering terlalu tajam (rebanding) atau terlalu rata.
     Kulit asli punya micro-texture dengan rentang ketajaman khas.

  3. COLOR / SPECULAR REFLECTION
     Layar memancarkan cahaya -> highlight specular + saturasi tinggi
     yang tidak natural. Kulit asli memantulkan cahaya secara difus.

  4. BLINK DETECTION (opsional, AKTIF, paling kuat anti-spoof foto diam)
     Hitung Eye Aspect Ratio (EAR) dari landmark mata lintas-frame.
     Foto diam tidak pernah berkedip -> EAR konstan. Orang asli berkedip.
     Butuh face_recognition landmark; kalau tidak ada, cue ini di-skip.

Hasil akhir: ``LivenessResult(is_live, score, reason, details)``.
``is_live = score >= threshold``. Threshold default konservatif (0.5).

Catatan: passive liveness tidak 100% anti-spoof (tidak ada yang 100%).
Ini lapisan pertahanan tambahan yang murah + ringan untuk gate presensi,
bukan pengganti hardware depth/IR. Untuk keamanan tinggi, kombinasikan
dengan kamera depth (mis. Intel RealSense) atau active challenge.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger("sapa.edge.liveness")

try:
    import cv2  # type: ignore
    _HAS_CV2 = True
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore
    _HAS_CV2 = False

try:
    import face_recognition  # type: ignore
    _HAS_FACE_RECOGNITION = True
except Exception:  # pragma: no cover
    face_recognition = None  # type: ignore
    _HAS_FACE_RECOGNITION = False


@dataclass
class LivenessResult:
    """Hasil analisis liveness satu wajah."""

    is_live: bool
    score: float                      # 0..1 (makin tinggi = makin "asli")
    reason: str                       # ringkasan keputusan
    details: Dict[str, float] = field(default_factory=dict)


@dataclass
class _BlinkTrack:
    """State blink-detection per posisi wajah (sederhana, single-face)."""

    ear_history: Deque[float] = field(default_factory=lambda: deque(maxlen=30))
    blink_count: int = 0
    last_below: bool = False
    last_update: float = 0.0


class LivenessDetector:
    """Detektor liveness passive multi-cue.

    Pemakaian per frame:
        det = LivenessDetector(threshold=0.5)
        result = det.analyze(frame_bgr, bbox, landmarks=optional)
        if result.is_live: ...  # wajah asli
    """

    # Eye Aspect Ratio threshold: di bawah ini dianggap mata tertutup (kedip).
    EAR_CLOSED_THRESHOLD = 0.21
    # Minimal blink agar dianggar "ada gerakan hidup" (kalau blink aktif).
    MIN_BLINKS_FOR_LIVE = 1
    # Berapa lama (detik) menunggu blink sebelum memutuskan tanpa blink-cue.
    BLINK_GRACE_SECONDS = 3.0

    def __init__(
        self,
        threshold: float = 0.5,
        use_blink: bool = True,
        require_blink: bool = False,
    ) -> None:
        """
        Args:
          threshold: skor minimum agar dianggap live (0..1).
          use_blink: aktifkan blink detection (butuh landmark mata).
          require_blink: kalau True, WAJIB ada blink dalam grace period
            untuk lolos (paling ketat, anti foto diam). Kalau False,
            blink hanya menambah skor (passive cues tetap bisa meloloskan).
        """
        self.threshold = float(threshold)
        self.use_blink = bool(use_blink) and _HAS_FACE_RECOGNITION
        self.require_blink = bool(require_blink)
        self._blink = _BlinkTrack()

    # ---------------------------------------------------------------------
    # API utama
    # ---------------------------------------------------------------------
    def analyze(
        self,
        frame_bgr: np.ndarray,
        bbox: Tuple[int, int, int, int],
        landmarks: Optional[Dict[str, List[Tuple[int, int]]]] = None,
    ) -> LivenessResult:
        """Analisis satu wajah.

        Args:
          frame_bgr: frame penuh (BGR, dari cv2.VideoCapture).
          bbox: (top, right, bottom, left) — format face_recognition.
          landmarks: dict landmark (opsional) untuk blink. Kalau None dan
            use_blink aktif, akan dicoba dihitung sendiri dari crop.

        Returns:
          LivenessResult.
        """
        if not _HAS_CV2 or frame_bgr is None:
            # Tanpa cv2 tidak bisa analisis -> default lolos (jangan blokir gate)
            return LivenessResult(True, 1.0, "cv2_unavailable_skip", {})

        crop = self._crop_face(frame_bgr, bbox)
        if crop is None or crop.size == 0:
            return LivenessResult(False, 0.0, "empty_crop", {})

        details: Dict[str, float] = {}

        # Cue 1: moiré / frekuensi tinggi
        moire_score = self._moire_score(crop)
        details["moire"] = round(moire_score, 3)

        # Cue 2: tekstur / sharpness
        texture_score = self._texture_score(crop)
        details["texture"] = round(texture_score, 3)

        # Cue 3: warna / specular reflection
        color_score = self._color_score(crop)
        details["color"] = round(color_score, 3)

        # Gabungan passive cue (bobot: moiré paling penting untuk anti-layar)
        passive = (
            0.50 * moire_score
            + 0.30 * texture_score
            + 0.20 * color_score
        )
        details["passive"] = round(passive, 3)

        # Cue 4: blink (opsional, aktif)
        blink_ok = None
        if self.use_blink:
            blink_ok, ear = self._update_blink(frame_bgr, bbox, landmarks)
            details["ear"] = round(ear, 3) if ear is not None else -1.0
            details["blinks"] = float(self._blink.blink_count)

        # Keputusan akhir
        score = passive
        reason_parts = [f"passive={passive:.2f}"]

        if self.use_blink and blink_ok is not None:
            if blink_ok:
                # Blink terdeteksi -> kuat indikasi hidup, naikkan skor
                score = min(1.0, passive + 0.30)
                reason_parts.append("blink=yes")
            else:
                reason_parts.append("blink=waiting")
                if self.require_blink:
                    # Mode ketat: belum ada blink dalam grace -> tolak
                    elapsed = time.time() - (self._blink.last_update or time.time())
                    if self._blink.blink_count < self.MIN_BLINKS_FOR_LIVE:
                        score = min(score, self.threshold - 0.01)
                        reason_parts.append("require_blink_unmet")

        is_live = score >= self.threshold
        reason = ("LIVE" if is_live else "SPOOF") + " (" + ", ".join(reason_parts) + ")"
        return LivenessResult(is_live=is_live, score=round(float(score), 3),
                              reason=reason, details=details)

    def reset_blink(self) -> None:
        """Reset state blink (panggil saat wajah berganti / tidak ada wajah)."""
        self._blink = _BlinkTrack()

    # ---------------------------------------------------------------------
    # Helper: crop wajah dari frame berdasar bbox face_recognition
    # ---------------------------------------------------------------------
    @staticmethod
    def _crop_face(
        frame_bgr: np.ndarray,
        bbox: Tuple[int, int, int, int],
    ) -> Optional[np.ndarray]:
        """Potong region wajah dari frame.

        bbox format face_recognition = (top, right, bottom, left).
        Diberi sedikit margin + clamp ke batas frame supaya aman.
        """
        try:
            top, right, bottom, left = bbox
            h, w = frame_bgr.shape[:2]
            # margin 10% tinggi/lebar wajah
            fh = max(1, bottom - top)
            fw = max(1, right - left)
            my = int(fh * 0.1)
            mx = int(fw * 0.1)
            y0 = max(0, top - my)
            y1 = min(h, bottom + my)
            x0 = max(0, left - mx)
            x1 = min(w, right + mx)
            if y1 <= y0 or x1 <= x0:
                return None
            return frame_bgr[y0:y1, x0:x1]
        except Exception:
            return None

    # ---------------------------------------------------------------------
    # Cue 1: Moiré / frequency analysis
    # ---------------------------------------------------------------------
    def _moire_score(self, crop_bgr: np.ndarray) -> float:
        """Skor 0..1: tinggi = kemungkinan ASLI (sedikit energi moiré).

        Layar memantulkan struktur piksel -> energi tinggi di pita frekuensi
        menengah-tinggi yang periodik. Kita ukur rasio energi frekuensi tinggi
        terhadap total via FFT pada grayscale.
        """
        try:
            gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
            # Normalisasi ukuran supaya konsisten
            gray = cv2.resize(gray, (128, 128), interpolation=cv2.INTER_AREA)
            gray = gray.astype(np.float32)
            # Hilangkan DC + windowing supaya tepi tidak bikin artefak frekuensi
            gray = gray - gray.mean()
            win = np.outer(np.hanning(128), np.hanning(128)).astype(np.float32)
            gray = gray * win

            f = np.fft.fftshift(np.fft.fft2(gray))
            mag = np.abs(f)
            total = float(mag.sum()) + 1e-6

            # Mask pita frekuensi tinggi (jauh dari pusat)
            cy, cx = 64, 64
            yy, xx = np.ogrid[:128, :128]
            radius = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
            high_mask = radius > 40  # pita tinggi
            high_energy = float(mag[high_mask].sum())

            high_ratio = high_energy / total  # makin besar = makin "berlayar"

            # Mapping: high_ratio kecil (wajah asli halus) -> skor tinggi.
            # Empiris: layar HP/laptop sering high_ratio > 0.35.
            # asli biasanya 0.10..0.25.
            if high_ratio <= 0.18:
                return 1.0
            if high_ratio >= 0.45:
                return 0.0
            # interpolasi linear terbalik
            return float(max(0.0, min(1.0, (0.45 - high_ratio) / (0.45 - 0.18))))
        except Exception as exc:
            logger.debug("moire_score error: %s", exc)
            return 0.5  # netral kalau gagal

    # ---------------------------------------------------------------------
    # Cue 2: Texture / sharpness (Laplacian variance)
    # ---------------------------------------------------------------------
    def _texture_score(self, crop_bgr: np.ndarray) -> float:
        """Skor 0..1: tinggi = ketajaman dalam rentang natural kulit.

        Terlalu blur (foto jelek/jauh) ATAU terlalu tajam (rebanding layar)
        sama-sama mencurigakan. Rentang natural kulit di tengah.
        """
        try:
            gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (128, 128), interpolation=cv2.INTER_AREA)
            lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

            # Empiris untuk crop 128x128:
            #   < 30   : terlalu blur (foto jauh / out of focus)  -> curiga
            #   60-400 : rentang natural kulit                     -> bagus
            #   > 900  : terlalu tajam (artefak layar/cetak)        -> curiga
            if lap_var < 15:
                return 0.1
            if lap_var < 60:
                # naik dari 0.1 -> 1.0 di rentang 15..60
                return float(0.1 + 0.9 * (lap_var - 15) / (60 - 15))
            if lap_var <= 400:
                return 1.0
            if lap_var <= 900:
                # turun dari 1.0 -> 0.3 di rentang 400..900
                return float(1.0 - 0.7 * (lap_var - 400) / (900 - 400))
            return 0.3
        except Exception as exc:
            logger.debug("texture_score error: %s", exc)
            return 0.5

    # ---------------------------------------------------------------------
    # Cue 3: Color / specular reflection
    # ---------------------------------------------------------------------
    def _color_score(self, crop_bgr: np.ndarray) -> float:
        """Skor 0..1: tinggi = distribusi warna natural kulit.

        Layar -> highlight specular (piksel sangat terang terkonsentrasi)
        dan saturasi tinggi tidak natural. Kulit asli lebih difus.
        """
        try:
            hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
            s = hsv[:, :, 1].astype(np.float32) / 255.0
            v = hsv[:, :, 2].astype(np.float32) / 255.0

            mean_sat = float(s.mean())
            # Proporsi piksel sangat terang (specular highlight dari layar)
            specular_ratio = float((v > 0.95).mean())

            score = 1.0
            # Saturasi natural kulit ~0.2..0.6. Di luar itu curiga.
            if mean_sat > 0.75 or mean_sat < 0.08:
                score -= 0.4
            # Banyak highlight specular -> indikasi layar
            if specular_ratio > 0.08:
                score -= 0.4
            elif specular_ratio > 0.04:
                score -= 0.2
            return float(max(0.0, min(1.0, score)))
        except Exception as exc:
            logger.debug("color_score error: %s", exc)
            return 0.5

    # ---------------------------------------------------------------------
    # Cue 4: Blink detection via Eye Aspect Ratio (EAR)
    # ---------------------------------------------------------------------
    def _update_blink(
        self,
        frame_bgr: np.ndarray,
        bbox: Tuple[int, int, int, int],
        landmarks: Optional[Dict[str, List[Tuple[int, int]]]],
    ) -> Tuple[bool, Optional[float]]:
        """Update state blink, return (sudah_pernah_blink, ear_terkini)."""
        try:
            if landmarks is None:
                landmarks = self._compute_landmarks(frame_bgr, bbox)
            if not landmarks:
                return (self._blink.blink_count >= self.MIN_BLINKS_FOR_LIVE, None)

            left = landmarks.get("left_eye")
            right = landmarks.get("right_eye")
            if not left or not right:
                return (self._blink.blink_count >= self.MIN_BLINKS_FOR_LIVE, None)

            ear_left = self._eye_aspect_ratio(left)
            ear_right = self._eye_aspect_ratio(right)
            ear = (ear_left + ear_right) / 2.0

            self._blink.ear_history.append(ear)
            self._blink.last_update = time.time()

            below = ear < self.EAR_CLOSED_THRESHOLD
            # Deteksi transisi tertutup->terbuka = satu kedipan
            if self._blink.last_below and not below:
                self._blink.blink_count += 1
            self._blink.last_below = below

            return (self._blink.blink_count >= self.MIN_BLINKS_FOR_LIVE, ear)
        except Exception as exc:
            logger.debug("blink update error: %s", exc)
            return (self._blink.blink_count >= self.MIN_BLINKS_FOR_LIVE, None)

    def _compute_landmarks(
        self,
        frame_bgr: np.ndarray,
        bbox: Tuple[int, int, int, int],
    ) -> Optional[Dict[str, List[Tuple[int, int]]]]:
        """Hitung landmark mata pakai face_recognition (kalau tersedia)."""
        if not _HAS_FACE_RECOGNITION:
            return None
        try:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            lm_list = face_recognition.face_landmarks(rgb, [bbox])
            if lm_list:
                return lm_list[0]
        except Exception:
            return None
        return None

    @staticmethod
    def _eye_aspect_ratio(eye: List[Tuple[int, int]]) -> float:
        """EAR = (jarak vertikal) / (jarak horizontal) mata.

        face_recognition mengembalikan 6 titik per mata. EAR turun drastis
        saat mata tertutup.
        """
        pts = np.array(eye, dtype=np.float32)
        if pts.shape[0] < 6:
            # face_recognition kadang kasih lebih dari 6 titik; ambil keliling
            # dengan sampling sederhana berbasis bounding.
            if pts.shape[0] < 4:
                return 1.0
        # Pakai 6 titik pertama dengan formula EAR standar (Soukupová & Čech)
        p = pts[:6]
        # vertikal: |p1-p5| + |p2-p4| ; horizontal: |p0-p3|
        v1 = np.linalg.norm(p[1] - p[5])
        v2 = np.linalg.norm(p[2] - p[4])
        h = np.linalg.norm(p[0] - p[3]) + 1e-6
        return float((v1 + v2) / (2.0 * h))
