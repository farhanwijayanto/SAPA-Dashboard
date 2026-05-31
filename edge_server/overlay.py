"""Overlay rendering untuk live preview di edge laptop.

Menggambar kotak HIJAU di sekitar wajah yang dikenali (employee_id valid)
dan kotak MERAH di sekitar wajah yang tidak dikenali. Plus label nama
dan confidence score di atas kotak.

Window OpenCV ini berjalan di laptop edge (bukan di browser dashboard) —
ditampilkan kepada operator di gate untuk konfirmasi visual.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore

logger = logging.getLogger("sapa.edge.overlay")

# BGR (OpenCV pakai BGR, bukan RGB)
COLOR_GREEN = (0, 200, 0)
COLOR_RED = (0, 0, 220)
COLOR_YELLOW = (0, 200, 220)
COLOR_WHITE = (255, 255, 255)
COLOR_BLACK = (0, 0, 0)

FONT = None  # diset di runtime kalau cv2 tersedia
if cv2 is not None:
    FONT = cv2.FONT_HERSHEY_SIMPLEX


@dataclass
class FaceAnnotation:
    """Hasil deteksi+matching satu wajah untuk digambar di overlay."""

    # Bounding box dalam koordinat frame (top, right, bottom, left)
    bbox: Tuple[int, int, int, int]
    # Kalau matched: employee_id + nama; kalau None -> "Unknown"
    employee_id: Optional[str]
    display_name: Optional[str]
    confidence: Optional[float]
    # True = dikenali (hijau), False = tidak dikenali (merah)
    is_valid: bool


def draw_face_box(frame: np.ndarray, ann: FaceAnnotation) -> None:
    """Gambar satu kotak + label di frame in-place.

    Frame mengikuti format BGR (output langsung dari ``cv2.VideoCapture``).
    Tidak return apapun — frame dimodifikasi langsung.
    """
    if cv2 is None or FONT is None:
        return

    top, right, bottom, left = ann.bbox

    # Pilih warna kotak
    color = COLOR_GREEN if ann.is_valid else COLOR_RED

    # Kotak utama (tebal 2 px)
    cv2.rectangle(frame, (left, top), (right, bottom), color, 2)

    # Label: "513061 - John Doe (91%)" atau "Unknown"
    if ann.is_valid and ann.display_name:
        conf_pct = int((ann.confidence or 0.0) * 100)
        label = f"{ann.employee_id or '?'} - {ann.display_name} ({conf_pct}%)"
    elif ann.is_valid and ann.employee_id:
        conf_pct = int((ann.confidence or 0.0) * 100)
        label = f"{ann.employee_id} ({conf_pct}%)"
    else:
        label = "Unknown"

    # Background panel untuk label di atas kotak
    (label_w, label_h), baseline = cv2.getTextSize(label, FONT, 0.6, 1)
    panel_top = max(top - label_h - 12, 0)
    panel_bottom = top
    panel_left = left
    panel_right = min(left + label_w + 12, frame.shape[1])

    # Filled panel untuk kontras teks
    cv2.rectangle(
        frame,
        (panel_left, panel_top),
        (panel_right, panel_bottom),
        color,
        thickness=cv2.FILLED,
    )

    # Tulis label
    text_org = (panel_left + 6, panel_bottom - 6)
    cv2.putText(frame, label, text_org, FONT, 0.6, COLOR_WHITE, 1, cv2.LINE_AA)


def draw_status_banner(
    frame: np.ndarray,
    *,
    fps: Optional[float] = None,
    synced_count: Optional[int] = None,
    last_match_id: Optional[str] = None,
    mqtt_connected: bool = False,
) -> None:
    """Banner status kecil di pojok kiri-atas frame."""
    if cv2 is None or FONT is None:
        return

    h, w = frame.shape[:2]
    banner_h = 60
    overlay = frame.copy()
    cv2.rectangle(overlay, (0, 0), (w, banner_h), COLOR_BLACK, thickness=cv2.FILLED)
    # Alpha blend supaya semi-transparent
    cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

    # Line 1: SAPA Edge + sync count + MQTT status
    parts_l1 = ["SAPA Edge"]
    if synced_count is not None:
        parts_l1.append(f"faces={synced_count}")
    parts_l1.append("MQTT=" + ("OK" if mqtt_connected else "DOWN"))
    if fps is not None:
        parts_l1.append(f"{fps:.1f} fps")
    line1 = "  |  ".join(parts_l1)
    cv2.putText(frame, line1, (10, 22), FONT, 0.55, COLOR_WHITE, 1, cv2.LINE_AA)

    # Line 2: last match
    line2 = (
        f"Last match: {last_match_id}"
        if last_match_id
        else "Last match: -"
    )
    cv2.putText(frame, line2, (10, 46), FONT, 0.5, COLOR_YELLOW, 1, cv2.LINE_AA)


def draw_help_footer(frame: np.ndarray) -> None:
    """Help text di pojok kiri-bawah."""
    if cv2 is None or FONT is None:
        return
    h, w = frame.shape[:2]
    txt = "[Q] quit  |  [R] resync  |  GREEN=match  RED=unknown"
    cv2.putText(
        frame,
        txt,
        (10, h - 12),
        FONT,
        0.5,
        COLOR_WHITE,
        1,
        cv2.LINE_AA,
    )
