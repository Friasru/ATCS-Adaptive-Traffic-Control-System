"""Draw detections, counting lines and count overlays onto frames (OpenCV)."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Mapping, Optional

import cv2
import numpy as np

from app.detection.classes import CLASS_COLORS_BGR, MONITORED_CLASSES
from app.detection.types import Detection
from app.tracking.counter import CountingLine

FONT = cv2.FONT_HERSHEY_SIMPLEX
SHORT = {"person": "Ppl", "car": "Car", "motorcycle": "Moto", "bicycle": "Bike", "bus": "Bus", "truck": "Truck"}


def _scale(img: np.ndarray) -> float:
    return max(0.4, min(img.shape[0], img.shape[1]) / 720.0)


def draw_detections(img: np.ndarray, detections: Iterable[Detection]) -> None:
    s = _scale(img)
    thick = max(1, int(round(2 * s)))
    fs = 0.5 * s
    for d in detections:
        color = CLASS_COLORS_BGR.get(d.cls, (255, 255, 255))
        p1, p2 = (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2))
        cv2.rectangle(img, p1, p2, color, thick)
        label = f"{d.cls}" + (f" #{d.track_id}" if d.track_id is not None else "") + f" {d.confidence:.2f}"
        (tw, th), base = cv2.getTextSize(label, FONT, fs, 1)
        y = p1[1] - 4 if p1[1] - th - 6 > 0 else p1[1] + th + 4
        cv2.rectangle(img, (p1[0], y - th - 3), (p1[0] + tw + 4, y + base - 1), color, -1)
        cv2.putText(img, label, (p1[0] + 2, y - 1), FONT, fs, (255, 255, 255), 1, cv2.LINE_AA)


def draw_lines(img: np.ndarray, lines: Iterable[CountingLine], totals: Optional[Mapping] = None) -> None:
    h, w = img.shape[:2]
    s = _scale(img)
    for ln in lines:
        if not ln.enabled:
            continue
        a, b = ln.pixels(w, h)
        pa, pb = (int(a[0]), int(a[1])), (int(b[0]), int(b[1]))
        cv2.line(img, pa, pb, (0, 255, 255), max(2, int(3 * s)), cv2.LINE_AA)
        for p, tag in ((pa, "P1"), (pb, "P2")):
            cv2.circle(img, p, max(4, int(6 * s)), (0, 255, 255), -1)
            cv2.putText(img, tag, (p[0] + 6, p[1] - 6), FONT, 0.45 * s, (0, 255, 255), 1, cv2.LINE_AA)
        # Side labels: A on the right-hand side of P1->P2, B on the left.
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = max(1e-6, (dx * dx + dy * dy) ** 0.5)
        nx, ny = -dy / n, dx / n  # right-hand normal in y-down coordinates
        off = 22 * s
        for sign, tag in ((1, "A"), (-1, "B")):
            px, py = int(mx + sign * nx * off), int(my + sign * ny * off)
            cv2.putText(img, tag, (px - 5, py + 5), FONT, 0.6 * s, (0, 255, 255), 2, cv2.LINE_AA)
        if totals and ln.line_id in totals:
            t = totals[ln.line_id]
            txt = f"{ln.line_id}  A>B {sum(t['a_to_b'].values())}  B>A {sum(t['b_to_a'].values())}"
            cv2.putText(img, txt, (pa[0], pa[1] + int(24 * s)), FONT, 0.5 * s, (0, 255, 255), 1, cv2.LINE_AA)


def draw_overlay(img: np.ndarray, visible: Mapping[str, int], crossings: Mapping[str, int],
                 recording: bool, timestamp: Optional[float] = None) -> None:
    """Count panel + timestamp + REC indicator (burned into recordings)."""
    s = _scale(img)
    fs = 0.45 * s
    line_h = int(18 * s)
    rows = [
        "Visible  " + "  ".join(f"{SHORT[c]}:{visible.get(c, 0)}" for c in MONITORED_CLASSES),
        "Crossed  " + "  ".join(f"{SHORT[c]}:{crossings.get(c, 0)}" for c in MONITORED_CLASSES),
    ]
    stamp = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp else ""
    widths = [cv2.getTextSize(r, FONT, fs, 1)[0][0] for r in rows + [stamp]]
    panel_w = max(widths) + 12
    panel_h = line_h * 3 + 8
    roi = img[0:panel_h, 0:min(panel_w, img.shape[1])]
    roi[:] = (roi * 0.45).astype(np.uint8)
    for i, text in enumerate(rows + [stamp]):
        cv2.putText(img, text, (6, line_h * (i + 1)), FONT, fs, (255, 255, 255), 1, cv2.LINE_AA)
    if recording:
        x = img.shape[1] - int(70 * s)
        cv2.circle(img, (x, int(16 * s)), int(7 * s), (0, 0, 255), -1)
        cv2.putText(img, "REC", (x + int(12 * s), int(22 * s)), FONT, 0.55 * s, (0, 0, 255), 2, cv2.LINE_AA)


def annotate(img: np.ndarray, detections, lines, line_totals, visible, crossings, recording: bool,
             timestamp: Optional[float], overlay: bool = True) -> np.ndarray:
    out = img.copy()
    draw_lines(out, lines, line_totals)
    draw_detections(out, detections)
    if overlay:
        draw_overlay(out, visible, crossings, recording, timestamp)
    return out
