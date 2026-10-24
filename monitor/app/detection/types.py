"""Plain data types shared by detection, tracking, counting and storage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Detection:
    """One object in one frame, in pixel coordinates of that frame."""

    cls: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float
    track_id: Optional[int] = None

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def bottom_center(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, self.y2)

    def anchor(self, mode: str) -> tuple[float, float]:
        return self.bottom_center if mode == "bottom_center" else self.center

    def is_valid(self, width: int, height: int) -> bool:
        return (
            0.0 <= self.confidence <= 1.0
            and self.x2 > self.x1
            and self.y2 > self.y1
            and -1.0 <= self.x1
            and -1.0 <= self.y1
            and self.x2 <= width + 1.0
            and self.y2 <= height + 1.0
        )
