"""Annotated video recorder running on its own thread.

Frames are written at a constant frame rate. Each incoming frame carries its
time on the source timeline; when processing is slower than the output rate the
previous frame is repeated ("duplicated") so the file's duration matches the
real elapsed time, and when frames arrive faster than the output rate the
surplus is skipped. Frames that could not be queued because the encoder fell
behind are counted as ``dropped`` and reported - never hidden.
"""

from __future__ import annotations

import logging
import os
import queue
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

# (fourcc, extension) in order of preference.
CODECS: tuple[tuple[str, str], ...] = (("mp4v", ".mp4"), ("MJPG", ".avi"))
MIN_FREE_BYTES_START = 200 * 1024 * 1024
MIN_FREE_BYTES_RUNNING = 50 * 1024 * 1024
MAX_DUPLICATES_PER_GAP = 120


class RecordingError(RuntimeError):
    pass


@dataclass
class RecordingResult:
    path: str
    codec: str
    container: str
    fps: float
    width: int
    height: int
    frames_received: int = 0
    frames_written: int = 0
    frames_duplicated: int = 0
    frames_skipped: int = 0
    frames_dropped: int = 0
    started_at: str = ""
    ended_at: str = ""
    duration_s: float = 0.0
    ok: bool = False
    error: Optional[str] = None
    validation: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def unique_recording_path(directory: Path, session_id: str, ext: str, now: Optional[datetime] = None) -> Path:
    """Reserve a new file name; never returns an existing file."""
    now = now or datetime.now()
    stem = f"atcs_{now.strftime('%Y%m%d-%H%M%S')}_{session_id[:8]}"
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(1000):
        candidate = directory / (f"{stem}{ext}" if n == 0 else f"{stem}_{n}{ext}")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise RecordingError("Could not reserve a unique recording file name")


def validate_video(path: str | os.PathLike, expected_frames: Optional[int] = None) -> dict:
    """Open a finished recording and check that it is really decodable."""
    p = Path(path)
    info: dict = {"exists": p.is_file(), "size_bytes": p.stat().st_size if p.is_file() else 0}
    info.update(opened=False, frame_count=0, decoded_frames=0, first_frame_ok=False, last_frame_ok=False, fps=0.0, duration_s=0.0)
    if not info["exists"] or info["size_bytes"] == 0:
        info["ok"] = False
        return info
    cap = cv2.VideoCapture(str(p))
    try:
        info["opened"] = bool(cap.isOpened())
        if not info["opened"]:
            info["ok"] = False
            return info
        info["fps"] = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        info["frame_count"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        decoded = 0
        first_ok = last_ok = False
        while True:
            ok, img = cap.read()
            if not ok or img is None:
                break
            valid = img.size > 0 and img.ndim == 3
            if decoded == 0:
                first_ok = valid
            last_ok = valid
            decoded += 1
        info["decoded_frames"] = decoded
        info["first_frame_ok"] = first_ok
        info["last_frame_ok"] = last_ok
        info["duration_s"] = decoded / info["fps"] if info["fps"] > 0 else 0.0
    finally:
        cap.release()
    ok = info["opened"] and info["decoded_frames"] > 0 and info["first_frame_ok"] and info["last_frame_ok"]
    if expected_frames is not None:
        info["expected_frames"] = expected_frames
        # Container frame counts can differ by a frame or two; decoded count must match closely.
        ok = ok and abs(info["decoded_frames"] - expected_frames) <= max(2, int(0.01 * expected_frames))
    info["ok"] = bool(ok)
    return info


class VideoRecorder:
    def __init__(
        self,
        directory: Path,
        session_id: str,
        fps: float,
        frame_size: tuple[int, int],
        queue_size: int = 64,
        codecs: tuple[tuple[str, str], ...] = CODECS,
    ):
        if fps <= 0 or fps > 240:
            raise RecordingError(f"Invalid recording frame rate {fps}")
        w, h = frame_size
        if w <= 0 or h <= 0:
            raise RecordingError(f"Invalid frame size {frame_size}")
        self.directory = Path(directory)
        self.session_id = session_id
        self.fps = float(fps)
        self.size = (int(w), int(h))
        self.codecs = codecs
        self._q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._thread: Optional[threading.Thread] = None
        self._writer: Optional[cv2.VideoWriter] = None
        self._error: Optional[str] = None
        self._lock = threading.Lock()
        self.result: Optional[RecordingResult] = None
        self._stopped = False

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> RecordingResult:
        try:
            free = shutil.disk_usage(self.directory if self.directory.exists() else self.directory.parent).free
        except OSError as exc:
            raise RecordingError(f"Cannot access recordings directory: {exc}") from exc
        if free < MIN_FREE_BYTES_START:
            raise RecordingError(f"Not enough free disk space to record ({free // (1024 * 1024)} MB free)")
        last_err = "no codec available"
        for fourcc, ext in self.codecs:
            try:
                path = unique_recording_path(self.directory, self.session_id, ext)
            except OSError as exc:
                raise RecordingError(f"Cannot create recording file: {exc}") from exc
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), self.fps, self.size)
            if writer.isOpened():
                self._writer = writer
                self.result = RecordingResult(
                    path=str(path), codec=fourcc, container=ext.lstrip("."), fps=self.fps,
                    width=self.size[0], height=self.size[1],
                    started_at=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                )
                break
            writer.release()
            last_err = f"encoder {fourcc} unavailable"
            try:
                if path.stat().st_size == 0:
                    path.unlink()  # remove only the empty placeholder we created
            except OSError:
                pass
        if self._writer is None:
            raise RecordingError(f"No working video encoder ({last_err})")
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()
        assert self.result is not None
        log.info("Recording to %s (%s, %.1f fps, %dx%d)", self.result.path, self.result.codec, self.fps, *self.size)
        return self.result

    def push(self, image: np.ndarray, media_time: float) -> bool:
        """Queue a frame; never blocks the pipeline. Returns False if dropped."""
        if self.result is None or self._stopped:
            return False
        self.result.frames_received += 1
        if self._error is not None:
            self.result.frames_dropped += 1
            return False
        try:
            self._q.put_nowait((image, media_time))
            return True
        except queue.Full:
            self.result.frames_dropped += 1
            return False

    def _run(self) -> None:
        assert self.result is not None and self._writer is not None
        res = self.result
        t0: Optional[float] = None
        last: Optional[np.ndarray] = None
        next_disk_check = 100
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                image, media_time = item
                if image.shape[1] != self.size[0] or image.shape[0] != self.size[1]:
                    image = cv2.resize(image, self.size)
                if t0 is None:
                    t0 = media_time
                target = int(round((media_time - t0) * self.fps)) + 1  # frames that should exist after this one
                if target <= res.frames_written:
                    res.frames_skipped += 1
                    continue
                gap = target - res.frames_written - 1
                if last is not None and gap > 0:
                    for _ in range(min(gap, MAX_DUPLICATES_PER_GAP)):
                        self._writer.write(last)
                        res.frames_written += 1
                        res.frames_duplicated += 1
                self._writer.write(image)
                res.frames_written += 1
                last = image
                # Threshold, not modulo: duplicated frames can step past a multiple of 100.
                if res.frames_written >= next_disk_check:
                    next_disk_check = res.frames_written + 100
                    self._check_disk(res)
        except Exception as exc:  # exception boundary: report, never hide
            log.exception("Recorder failure")
            self._error = f"Recording failed: {exc}"
            # Drain so producers never block.
            while True:
                try:
                    if self._q.get_nowait() is None:
                        break
                except queue.Empty:
                    time.sleep(0.01)
                    if self._stopped:
                        break

    def _check_disk(self, res: RecordingResult) -> None:
        free = shutil.disk_usage(self.directory).free
        if free < MIN_FREE_BYTES_RUNNING:
            raise RecordingError(f"Disk almost full ({free // (1024 * 1024)} MB free); recording stopped")
        if not Path(res.path).exists():
            raise RecordingError("Recording file disappeared while writing")

    def stop(self, validate: bool = True) -> RecordingResult:
        with self._lock:
            if self.result is None:
                raise RecordingError("Recorder was never started")
            if self._stopped:
                return self.result
            self._stopped = True
        res = self.result
        if self._thread is not None:
            try:
                self._q.put(None, timeout=10)
            except queue.Full:
                self._error = self._error or "Recorder queue stalled while stopping"
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                self._error = self._error or "Recorder thread did not finish in time"
        if self._writer is not None:
            try:
                self._writer.release()
            except cv2.error as exc:
                self._error = self._error or f"Finalizing recording failed: {exc}"
            self._writer = None
        res.ended_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        res.duration_s = res.frames_written / self.fps if self.fps else 0.0
        res.error = self._error
        if validate:
            res.validation = validate_video(res.path, expected_frames=res.frames_written)
            res.ok = res.error is None and res.validation.get("ok", False)
            if res.error is None and not res.ok:
                res.error = "Recording file failed validation (not decodable)" if res.frames_written else "No frames were recorded"
        else:
            res.ok = res.error is None and res.frames_written > 0
        return res
