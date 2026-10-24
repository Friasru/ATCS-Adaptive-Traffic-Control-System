"""User settings: validated, persisted as JSON in data/settings.json."""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from app import paths
from app.detection.classes import MONITORED_CLASSES
from app.tracking.counter import ANCHORS, CountingLine

log = logging.getLogger(__name__)

DEVICES = ("auto", "cpu", "gpu")
TRACKERS = ("bytetrack", "botsort")
LOG_LEVELS = ("none", "tracks", "all")
RESOLUTIONS = ((640, 480), (1280, 720), (1920, 1080))


@dataclass
class Settings:
    model_name: str = "yolo26n.pt"
    confidence: float = 0.35
    iou: float = 0.5
    imgsz: int = 0  # 0 = auto: 960 on GPU, 640 on CPU (see effective_imgsz)
    max_processing_fps: float = 15.0  # 0 = unlimited
    device: str = "auto"
    tracker: str = "bytetrack"
    enabled_classes: list = field(default_factory=lambda: list(MONITORED_CLASSES))
    camera_width: int = 640
    camera_height: int = 480
    min_hits: int = 3
    anchor: str = "center"
    hysteresis_frac: float = 0.01
    min_travel_frac: float = 0.4  # travel needed to count a crossing, x object size (see counter.py)
    event_log_level: str = "tracks"
    recording_overlay: bool = True
    lines_enabled: bool = True
    lines: list = field(default_factory=lambda: [CountingLine().to_dict()])

    def validate(self) -> None:
        def finite(name, lo, hi):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or not lo <= v <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi} (got {v!r})")

        if not isinstance(self.model_name, str) or not self.model_name.endswith((".pt", ".onnx")) or "/" in self.model_name or "\\" in self.model_name:
            raise ValueError("model_name must be a bare .pt/.onnx file name inside models/")
        finite("confidence", 0.01, 0.99)
        finite("iou", 0.1, 0.95)
        if (not isinstance(self.imgsz, int) or isinstance(self.imgsz, bool)
                or (self.imgsz != 0 and (self.imgsz < 160 or self.imgsz > 1920 or self.imgsz % 32))):
            raise ValueError("imgsz must be 0 (auto) or a multiple of 32 between 160 and 1920")
        finite("max_processing_fps", 0, 120)
        if self.device not in DEVICES:
            raise ValueError(f"device must be one of {DEVICES}")
        if self.tracker not in TRACKERS:
            raise ValueError(f"tracker must be one of {TRACKERS}")
        if not isinstance(self.enabled_classes, list) or any(c not in MONITORED_CLASSES for c in self.enabled_classes):
            raise ValueError("enabled_classes contains an unknown class")
        if not self.enabled_classes:
            raise ValueError("At least one object class must be enabled")
        for name in ("camera_width", "camera_height"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or not 160 <= v <= 3840:
                raise ValueError(f"{name} must be an integer between 160 and 3840")
        if not isinstance(self.min_hits, int) or not 1 <= self.min_hits <= 30:
            raise ValueError("min_hits must be between 1 and 30")
        if self.anchor not in ANCHORS:
            raise ValueError(f"anchor must be one of {ANCHORS}")
        finite("hysteresis_frac", 0.0, 0.2)
        finite("min_travel_frac", 0.0, 2.0)
        if self.event_log_level not in LOG_LEVELS:
            raise ValueError(f"event_log_level must be one of {LOG_LEVELS}")
        ids = set()
        for d in self.lines:
            ln = CountingLine.from_dict(d)
            if ln.line_id in ids:
                raise ValueError(f"Duplicate line id {ln.line_id}")
            ids.add(ln.line_id)

    def effective_imgsz(self, device: str) -> int:
        """Auto size: 960 px finds small/distant objects much better (measured in
        docs/performance.md) and is cheap on a GPU; 640 px keeps CPUs real-time."""
        if self.imgsz:
            return self.imgsz
        return 960 if device.startswith("cuda") else 640

    def counting_lines(self) -> list[CountingLine]:
        return [CountingLine.from_dict(d) for d in self.lines]

    def copy(self) -> "Settings":
        return Settings.from_dict(asdict(self), validate=False)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict, validate: bool = True) -> "Settings":
        known = {f.name for f in fields(cls)}
        s = cls(**{k: v for k, v in d.items() if k in known})
        if validate:
            s.validate()
        return s


def load_settings(path: Optional[Path] = None) -> Settings:
    """Load settings; fall back to defaults if the file is missing or invalid."""
    path = path or paths.settings_path()
    if not path.exists():
        return Settings()
    try:
        return Settings.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.warning("Ignoring invalid settings file %s: %s", path, exc)
        return Settings()


def save_settings(settings: Settings, path: Optional[Path] = None) -> None:
    settings.validate()
    path = path or paths.settings_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
