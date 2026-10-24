"""Video display widget: aspect-ratio preserving, with counting-line editing."""

from __future__ import annotations

from typing import Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.tracking.counter import CountingLine

HANDLE_RADIUS = 8


def bgr_to_qimage(img: np.ndarray) -> QImage:
    h, w = img.shape[:2]
    rgb = np.ascontiguousarray(img[:, :, ::-1])
    # .copy() detaches the QImage from the numpy buffer.
    return QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()


class CameraView(QWidget):
    """Shows the latest annotated frame. In edit mode the user can drag the
    two endpoints (P1, P2) of the counting line; positions are normalised to
    the image so they are independent of window size and camera resolution."""

    lineChanged = Signal(object)  # CountingLine

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._image: Optional[QImage] = None
        self._message = "No source - choose a camera or video file and press Start"
        self._line: Optional[CountingLine] = None
        self._edit = False
        self._drag: Optional[int] = None  # 0 -> p1, 1 -> p2

    # ----------------------------------------------------------- public API
    def set_frame(self, img: Optional[np.ndarray]) -> None:
        self._image = bgr_to_qimage(img) if img is not None else None
        self.update()

    def set_message(self, text: str) -> None:
        self._message = text
        self.update()

    def clear(self, message: str = "") -> None:
        self._image = None
        if message:
            self._message = message
        self.update()

    def set_line(self, line: Optional[CountingLine]) -> None:
        self._line = line
        self.update()

    def set_edit_mode(self, on: bool) -> None:
        self._edit = on
        self._drag = None
        self.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        self.update()

    @property
    def edit_mode(self) -> bool:
        return self._edit

    def has_frame(self) -> bool:
        return self._image is not None

    # -------------------------------------------------------------- geometry
    def image_rect(self) -> QRectF:
        """Letter-boxed rectangle in which the frame is drawn."""
        if self._image is None:
            w, h = 16, 9
        else:
            w, h = self._image.width(), self._image.height()
        scale = min(self.width() / w, self.height() / h)
        dw, dh = w * scale, h * scale
        return QRectF((self.width() - dw) / 2, (self.height() - dh) / 2, dw, dh)

    def to_widget(self, p) -> QPointF:
        r = self.image_rect()
        return QPointF(r.x() + p[0] * r.width(), r.y() + p[1] * r.height())

    def to_normalized(self, pt: QPointF) -> tuple[float, float]:
        r = self.image_rect()
        x = (pt.x() - r.x()) / r.width() if r.width() else 0.0
        y = (pt.y() - r.y()) / r.height() if r.height() else 0.0
        return (min(1.0, max(0.0, x)), min(1.0, max(0.0, y)))

    # ---------------------------------------------------------------- events
    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(24, 24, 24))
        r = self.image_rect()
        if self._image is not None:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            p.drawImage(r, self._image)
        else:
            p.setPen(QColor(170, 170, 170))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)
        if self._edit and self._line is not None:
            self._paint_line_editor(p)
        p.end()

    def _paint_line_editor(self, p: QPainter) -> None:
        a, b = self.to_widget(self._line.p1), self.to_widget(self._line.p2)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(255, 0, 255), 2, Qt.PenStyle.DashLine))
        p.drawLine(a, b)
        p.setFont(QFont(self.font().family(), 9, QFont.Weight.Bold))
        for pt, tag in ((a, "P1"), (b, "P2")):
            p.setBrush(QColor(255, 0, 255))
            p.drawEllipse(pt, HANDLE_RADIUS, HANDLE_RADIUS)
            p.drawText(pt + QPointF(10, -10), tag)
        # Side markers: A is on the right-hand side walking from P1 to P2.
        mid = (a + b) / 2
        d = b - a
        length = max(1e-6, (d.x() ** 2 + d.y() ** 2) ** 0.5)
        n = QPointF(-d.y() / length, d.x() / length)
        p.drawText(mid + n * 24 - QPointF(4, -4), "A")
        p.drawText(mid - n * 24 - QPointF(4, -4), "B")
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(self.rect()).adjusted(8, 4, -8, -4), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
                   "Editing counting line: drag P1 / P2. Side A is right of the arrow P1 -> P2.")

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if not self._edit or self._line is None or e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        best, dist = None, HANDLE_RADIUS * 2.5
        for i, pt in enumerate((self._line.p1, self._line.p2)):
            w = self.to_widget(pt)
            dd = ((w.x() - pos.x()) ** 2 + (w.y() - pos.y()) ** 2) ** 0.5
            if dd <= dist:
                best, dist = i, dd
        self._drag = best

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is None or self._line is None:
            return
        n = self.to_normalized(e.position())
        p1, p2 = (n, self._line.p2) if self._drag == 0 else (self._line.p1, n)
        self._line = CountingLine(self._line.line_id, p1, p2, self._line.direction, self._line.enabled)
        self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if self._drag is None or self._line is None:
            return
        self._drag = None
        try:
            self._line.validate()
        except ValueError:
            return  # e.g. endpoints dropped on top of each other; owner keeps last valid line
        self.lineChanged.emit(self._line)
