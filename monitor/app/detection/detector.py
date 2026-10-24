"""YOLO detection + multi-object tracking via the official Ultralytics API."""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional

import numpy as np

from app import paths
from app.detection.classes import MONITORED_CLASSES, resolve_class_ids
from app.detection.types import Detection

log = logging.getLogger(__name__)

TRACKER_CONFIGS = {"bytetrack": "bytetrack.yaml", "botsort": "botsort.yaml"}
# Ultralytics applies `conf` before the tracker. ByteTrack's second association
# stage needs the low-score boxes (track_low_thresh = 0.1 in bytetrack.yaml) to
# keep tracks alive through blur/occlusion, so the model runs at this floor and
# the user's threshold is applied afterwards (see Detector.track).
TRACKER_CONF_FLOOR = 0.1


class ModelError(RuntimeError):
    pass


def disable_ultralytics_analytics() -> None:
    """Turn off Ultralytics' anonymous usage analytics ("sync") in the
    project-local settings file. Its predictor callbacks would otherwise send
    events whenever the library considers itself online."""
    from ultralytics.utils import SETTINGS

    if SETTINGS.get("sync", True):
        SETTINGS.update({"sync": False})


def resolve_device(preference: str) -> str:
    """Map 'auto' | 'cpu' | 'gpu' to a torch device string."""
    import torch

    if preference == "cpu":
        return "cpu"
    has_cuda = torch.cuda.is_available()
    if preference == "gpu" and not has_cuda:
        raise ModelError("GPU requested but no CUDA device is available")
    return "cuda:0" if has_cuda else "cpu"


def file_sha256(path: Path, length: int = 12) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


def download_weights(name: str, dest_dir: Optional[Path] = None) -> Path:
    """Download official weights into models/ using Ultralytics' asset helper."""
    dest_dir = dest_dir or paths.models_dir()
    target = dest_dir / name
    if target.is_file():
        return target
    try:
        disable_ultralytics_analytics()
        from ultralytics.utils.downloads import attempt_download_asset

        result = Path(attempt_download_asset(str(target)))
    except Exception as exc:
        raise ModelError(f"Downloading model weights '{name}' failed: {exc}") from exc
    if not result.is_file() or result.stat().st_size < 1_000_000:
        raise ModelError(f"Downloading model weights '{name}' failed (file missing or truncated)")
    return result


class Detector:
    """Wraps an Ultralytics YOLO model. Not thread-safe: use from one thread."""

    def __init__(self, model_name: str, device_pref: str = "auto", tracker: str = "bytetrack",
                 models_dir: Optional[Path] = None, allow_download: bool = False):
        self.model_name = model_name
        self.device_pref = device_pref
        self.tracker = tracker
        self._explicit_models_dir = models_dir
        self.models_dir = models_dir or paths.models_dir()  # where a download goes
        self.allow_download = allow_download
        self.model = None
        self.device = "cpu"
        self.half = False
        self.class_ids: dict[str, int] = {}
        self.id_to_class: dict[int, str] = {}
        self.version = ""
        self.inference_failures = 0
        self._established: set[int] = set()  # track ids that reached the user threshold once
        self._lock = threading.Lock()

    @property
    def weights_path(self) -> Path:
        if self._explicit_models_dir is not None:
            return self._explicit_models_dir / self.model_name
        return paths.find_model(self.model_name)  # also finds weights bundled with an installed copy

    def load(self) -> None:
        path = self.weights_path
        if not path.is_file():
            if not self.allow_download:
                raise ModelError(
                    f"Model weights not found: {path}. Run 'scripts/download_model.py' "
                    "(needs internet once) or copy the file into the models/ folder."
                )
            path = download_weights(self.model_name, self.models_dir)
        try:
            import ultralytics
            from ultralytics import YOLO

            disable_ultralytics_analytics()

            model = YOLO(str(path), task="detect")
        except Exception as exc:
            raise ModelError(f"Cannot load model '{path.name}': {exc}") from exc
        self.class_ids = resolve_class_ids(model.names)
        self.id_to_class = {v: k for k, v in self.class_ids.items()}
        self.device = resolve_device(self.device_pref)
        self.half = self.device.startswith("cuda")
        self.model = model
        self.version = f"{path.name} sha256:{file_sha256(path)} ultralytics {ultralytics.__version__}"
        try:
            self._warmup()
        except Exception as exc:
            if self.device_pref == "auto" and self.device != "cpu":
                log.warning("GPU warm-up failed (%s); falling back to CPU", exc)
                self.device, self.half = "cpu", False
                self._warmup()
            else:
                raise ModelError(f"Model warm-up failed on {self.device}: {exc}") from exc
        self.reset_tracker()
        log.info("Loaded %s on %s", self.version, self.device)

    def _warmup(self) -> None:
        assert self.model is not None
        blank = np.zeros((480, 640, 3), dtype=np.uint8)
        self.model.predict(blank, imgsz=320, device=self.device, quantize=16 if self.half else None, verbose=False)

    def reset_tracker(self) -> None:
        """Forget all tracks (new session / new source). IDs restart at 1."""
        if self.model is None:
            return
        predictor = getattr(self.model, "predictor", None)
        trackers = getattr(predictor, "trackers", None) if predictor is not None else None
        if trackers:
            for t in trackers:
                t.reset()
        # Force Ultralytics to rebuild predictor + tracker on next call so a
        # tracker type change is also honoured.
        if predictor is not None:
            self.model.predictor = None
        self._established.clear()

    def track(self, image: np.ndarray, conf: float, iou: float, imgsz: int,
              classes: Optional[list[str]] = None) -> list[Detection]:
        if self.model is None:
            raise ModelError("Model is not loaded")
        wanted = classes if classes is not None else list(MONITORED_CLASSES)
        ids = [self.class_ids[c] for c in wanted if c in self.class_ids]
        if not ids:
            return []
        results = self.model.track(
            image,
            persist=True,
            tracker=TRACKER_CONFIGS.get(self.tracker, "bytetrack.yaml"),
            conf=min(conf, TRACKER_CONF_FLOOR),
            iou=iou,
            imgsz=imgsz,
            classes=ids,
            device=self.device,
            quantize=16 if self.half else None,
            verbose=False,
        )
        return self.filter_tracked(self.convert(results[0], image.shape[1], image.shape[0]), conf)

    def filter_tracked(self, dets: list[Detection], conf: float) -> list[Detection]:
        """Apply the user's confidence threshold to tracker output.

        A track becomes visible once one of its detections reaches ``conf``;
        afterwards it stays visible while the tracker keeps it alive with
        lower-scoring boxes. Untracked boxes must always reach ``conf``.
        """
        out = []
        for d in dets:
            if d.confidence >= conf:
                if d.track_id is not None:
                    self._established.add(d.track_id)
                out.append(d)
            elif d.track_id is not None and d.track_id in self._established:
                out.append(d)
        return out

    def detect(self, image: np.ndarray, conf: float, iou: float, imgsz: int) -> list[Detection]:
        """Detection only (no tracking) - used by validation scripts/tests."""
        if self.model is None:
            raise ModelError("Model is not loaded")
        results = self.model.predict(image, conf=conf, iou=iou, imgsz=imgsz, classes=list(self.class_ids.values()),
                                     device=self.device, quantize=16 if self.half else None, verbose=False)
        return self.convert(results[0], image.shape[1], image.shape[0])

    def convert(self, result, width: int, height: int) -> list[Detection]:
        """Ultralytics Results -> list[Detection] restricted to monitored classes."""
        boxes = getattr(result, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return []
        xyxy = boxes.xyxy.cpu().numpy().astype(float)
        conf = boxes.conf.cpu().numpy().astype(float)
        cls = boxes.cls.cpu().numpy().astype(int)
        ids = boxes.id.cpu().numpy().astype(int) if getattr(boxes, "id", None) is not None else None
        out = []
        for i in range(len(xyxy)):
            name = self.id_to_class.get(int(cls[i]))
            if name is None:
                continue
            x1, y1, x2, y2 = (float(v) for v in xyxy[i])
            x1, x2 = max(0.0, min(x1, width)), max(0.0, min(x2, width))
            y1, y2 = max(0.0, min(y1, height)), max(0.0, min(y2, height))
            if x2 <= x1 or y2 <= y1:
                continue
            out.append(Detection(name, float(conf[i]), x1, y1, x2, y2, int(ids[i]) if ids is not None else None))
        return out
