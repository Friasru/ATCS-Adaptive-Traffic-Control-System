"""Frame sources: physical cameras and local video files.

Camera capture APIs: V4L2 on Linux; DirectShow, then Media Foundation on Windows;
AVFoundation on macOS. ATCS_CAMERA_BACKEND (v4l2, dshow, msmf, avfoundation, any)
forces a single API.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Media Foundation otherwise spends several seconds per open setting up hardware
# transforms. Read by OpenCV on first MSMF use, so it must be set before that.
os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = (".mp4", ".avi", ".mov", ".mkv", ".webm", ".m4v", ".mpg", ".mpeg", ".wmv")


class SourceError(RuntimeError):
    """Raised when a source cannot be opened or stops delivering frames."""


@dataclass(frozen=True)
class CameraInfo:
    index: int
    name: str
    backend: int = cv2.CAP_ANY  # capture API that delivered a frame during discovery

    @property
    def label(self) -> str:
        return f"{self.name} (#{self.index})"


@dataclass
class Frame:
    image: np.ndarray
    index: int  # 0-based frame number from this source
    wall_time: float  # time.time() when captured
    media_time: float  # seconds on the source timeline (file position or monotonic for cameras)


BACKENDS: dict[str, int] = {
    "v4l2": cv2.CAP_V4L2,
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "avfoundation": cv2.CAP_AVFOUNDATION,
    "any": cv2.CAP_ANY,
}


def backend_name(api: int) -> str:
    return next((k for k, v in BACKENDS.items() if v == api), str(api))


def _has_backend(api: int) -> bool:
    """Whether this OpenCV build can use the API (built in, or a loadable plugin)."""
    try:
        return bool(cv2.videoio_registry.hasBackend(api))
    except (AttributeError, cv2.error):
        return True  # cannot tell; let open() decide


def camera_backends() -> list[int]:
    """Capture APIs to try, in order of preference."""
    forced = os.environ.get("ATCS_CAMERA_BACKEND", "").strip().lower()
    if forced:
        if forced in BACKENDS:
            if not _has_backend(BACKENDS[forced]):
                log.warning("ATCS_CAMERA_BACKEND=%s is not available in this OpenCV build", forced)
            return [BACKENDS[forced]]
        log.warning("Ignoring unknown ATCS_CAMERA_BACKEND=%r (use one of %s)", forced, ", ".join(BACKENDS))
    if sys.platform.startswith("linux"):
        return [cv2.CAP_V4L2]
    if sys.platform == "win32":
        # DirectShow opens quickly and its device order matches the names below;
        # Media Foundation covers cameras that only ship an MF driver. The
        # opencv-python 5.0 wheels are built without MSMF, so it is skipped there.
        return [b for b in (cv2.CAP_DSHOW, cv2.CAP_MSMF) if _has_backend(b)] or [cv2.CAP_ANY]
    if sys.platform == "darwin":
        return [cv2.CAP_AVFOUNDATION]
    return [cv2.CAP_ANY]


def _windows_camera_names() -> list[str]:
    """DirectShow video input names, in the same order as CAP_DSHOW indices."""
    try:
        import comtypes
        from pygrabber.dshow_graph import FilterGraph
    except ImportError:
        return []
    initialized = False
    try:
        comtypes.CoInitialize()  # discovery runs on a worker thread
        initialized = True
        return [str(n) for n in FilterGraph().get_input_devices()]
    except Exception as exc:
        log.info("DirectShow device names unavailable (%s); using generic names", exc)
        return []
    finally:
        if initialized:
            comtypes.CoUninitialize()


def discover_cameras(max_index: int = 10) -> list[CameraInfo]:
    """List usable cameras. Never assumes index 0 exists."""
    found: list[CameraInfo] = []
    backends = camera_backends()
    if sys.platform.startswith("linux") and backends == [cv2.CAP_V4L2]:
        nodes = sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or 0))
        for node in nodes:
            idx = int(re.sub(r"\D", "", node) or 0)
            name_file = f"/sys/class/video4linux/video{idx}/name"
            try:
                with open(name_file, encoding="utf-8") as fh:
                    name = fh.read().strip() or f"Camera {idx}"
            except OSError:
                name = f"Camera {idx}"
            # Many webcams expose a metadata node next to the capture node;
            # only keep nodes that actually deliver a frame.
            if _probe(idx, cv2.CAP_V4L2):
                found.append(CameraInfo(idx, name, cv2.CAP_V4L2))
        return found
    names = _windows_camera_names() if sys.platform == "win32" else []
    for api in backends:
        # With DirectShow names the device count is known; otherwise scan indices.
        count = len(names) if names and api == cv2.CAP_DSHOW else max_index
        for idx in range(count):
            if _probe(idx, api):
                name = names[idx] if idx < len(names) else f"Camera {idx}"
                found.append(CameraInfo(idx, name, api))
        if found:
            break  # one API is enough; the next would list the same devices again
    log.info("Camera discovery (%s): %s", "/".join(backend_name(b) for b in backends),
             [f"{c.label} via {backend_name(c.backend)}" for c in found] or "none")
    return found


def _probe(index: int, api: int) -> bool:
    cap = cv2.VideoCapture(index, api)
    try:
        if not cap.isOpened():
            return False
        ok, _ = cap.read()
        return bool(ok)
    except cv2.error:
        return False
    finally:
        cap.release()


class FrameSource:
    kind = "abstract"
    #: True if frames must not be dropped (files are processed frame-by-frame).
    lossless = False

    def open(self) -> None:
        raise NotImplementedError

    def read(self) -> Optional[Frame]:
        """Return next frame, None at a clean end of stream, raise SourceError on failure."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    @property
    def identifier(self) -> str:
        raise NotImplementedError

    @property
    def fps(self) -> float:
        return 0.0

    @property
    def is_open(self) -> bool:
        return False


class CameraSource(FrameSource):
    kind = "camera"
    lossless = False

    def __init__(self, index: int, width: int = 640, height: int = 480, name: str = "", max_failures: int = 30,
                 backend: Optional[int] = None):
        if not isinstance(index, int) or index < 0:
            raise SourceError(f"Invalid camera index: {index!r}")
        self.index = index
        #: Capture API to use; None tries camera_backends() in order. Set to the
        #: working API after open() so reconnects use the same one.
        self.backend = backend
        self.width = width
        self.height = height
        self.name = name or f"Camera {index}"
        self.max_failures = max_failures
        self._cap: Optional[cv2.VideoCapture] = None
        self._count = 0
        self._t0 = 0.0
        self._fps = 0.0
        self._first: Optional[np.ndarray] = None

    @property
    def identifier(self) -> str:
        return f"camera:{self.index}:{self.name}"

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self) -> None:
        if sys.platform.startswith("linux") and not os.path.exists(f"/dev/video{self.index}"):
            raise SourceError(f"Camera #{self.index} does not exist (no /dev/video{self.index})")
        err = ""
        for api in [self.backend] if self.backend is not None else camera_backends():
            cap = cv2.VideoCapture(self.index, api)
            if not cap.isOpened():
                cap.release()
                err = err or f"Camera #{self.index} could not be opened (in use by another program or no permission?)"
                continue
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, img = cap.read()
            if not ok or img is None:
                cap.release()
                err = f"Camera #{self.index} opened but delivers no frames (busy or unsupported format)"
                continue
            self.backend = api
            break
        else:
            raise SourceError(err)
        log.info("Camera #%d '%s' opened via %s at %dx%d", self.index, self.name, backend_name(api),
                 img.shape[1], img.shape[0])
        self._cap = cap
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self._fps = fps if 0.0 < fps <= 240.0 else 0.0  # DirectShow reports -1 (unknown)
        self._count = 0
        self._t0 = time.monotonic()
        self._first = img

    def read(self) -> Optional[Frame]:
        if self._cap is None:
            raise SourceError("Camera is not open")
        if self._first is not None:
            img, self._first = self._first, None
        else:
            failures = 0
            while True:
                ok, img = self._cap.read()
                if ok and img is not None:
                    break
                failures += 1
                if failures >= self.max_failures:
                    raise SourceError(f"Camera #{self.index} stopped delivering frames (disconnected?)")
                time.sleep(0.02)
        f = Frame(img, self._count, time.time(), time.monotonic() - self._t0)
        self._count += 1
        return f

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class VideoFileSource(FrameSource):
    kind = "video_file"
    lossless = True

    def __init__(self, path: str | os.PathLike, realtime: bool = True):
        self.path = Path(path)
        self.realtime = realtime
        self._cap: Optional[cv2.VideoCapture] = None
        self._fps = 0.0
        self._count = 0
        self._frame_total = 0
        self._t_start = 0.0
        self._time_offset = 0.0  # media time keeps increasing across replays

    @property
    def identifier(self) -> str:
        return f"file:{self.path.name}"

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_total(self) -> int:
        return self._frame_total

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self) -> None:
        if not self.path.is_file():
            raise SourceError(f"Video file not found: {self.path}")
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            cap.release()
            raise SourceError(f"Cannot decode video file '{self.path.name}' (unsupported format or codec)")
        ok, img = cap.read()
        if not ok or img is None:
            cap.release()
            raise SourceError(f"Video file '{self.path.name}' contains no decodable frames")
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self._fps = fps if 1.0 <= fps <= 240.0 else 25.0
        self._frame_total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._cap = cap
        self._count = 0
        self._t_start = time.monotonic()

    def read(self) -> Optional[Frame]:
        if self._cap is None:
            raise SourceError("Video file is not open")
        ok, img = self._cap.read()
        if not ok or img is None:
            return None  # end of video
        local = self._count / self._fps
        if self.realtime:
            delay = self._t_start + local - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        f = Frame(img, self._count, time.time(), self._time_offset + local)
        self._count += 1
        return f

    def rewind(self) -> None:
        if self._cap is not None:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._time_offset += self._count / self._fps
            self._count = 0
            self._t_start = time.monotonic()

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None
