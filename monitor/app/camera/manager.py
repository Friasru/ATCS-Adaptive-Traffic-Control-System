"""Capture thread: pulls frames from a FrameSource into a bounded buffer.

Cameras use a single-slot "latest frame" buffer: when inference is slower than
capture, stale frames are replaced (and counted as dropped) instead of piling up.
Video files are processed frame-by-frame through a small blocking queue so no
frame of the file is skipped.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable, Optional

from app.camera.sources import Frame, FrameSource, SourceError

log = logging.getLogger(__name__)

EOS = object()  # end-of-stream marker


class FrameBuffer:
    def __init__(self, lossless: bool, capacity: int = 4):
        self.lossless = lossless
        self.capacity = 1 if not lossless else capacity
        self._dq: collections.deque = collections.deque()
        self._cv = threading.Condition()
        self.dropped = 0

    def put(self, item, stop: threading.Event) -> bool:
        with self._cv:
            if self.lossless and item is not EOS:
                while len(self._dq) >= self.capacity and not stop.is_set():
                    self._cv.wait(0.1)
                if stop.is_set():
                    return False
            elif item is not EOS:
                while self._dq and self._dq[0] is not EOS:
                    self._dq.popleft()
                    self.dropped += 1
            self._dq.append(item)
            self._cv.notify_all()
            return True

    def get(self, timeout: float):
        with self._cv:
            if not self._dq:
                self._cv.wait(timeout)
            if not self._dq:
                return None
            item = self._dq.popleft()
            self._cv.notify_all()
            return item

    def __len__(self) -> int:
        return len(self._dq)


class RateMeter:
    """Frames-per-second over a sliding window."""

    def __init__(self, window: float = 2.0):
        self.window = window
        self._t: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def tick(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            self._t.append(now)
            while self._t and now - self._t[0] > self.window:
                self._t.popleft()

    @property
    def rate(self) -> float:
        with self._lock:
            if len(self._t) < 2:
                return 0.0
            now = time.monotonic()
            if now - self._t[-1] > self.window:
                return 0.0
            span = self._t[-1] - self._t[0]
            return (len(self._t) - 1) / span if span > 0 else 0.0


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        source: FrameSource,
        buffer: FrameBuffer,
        on_error: Callable[[str], None],
        loop: bool = False,
    ):
        super().__init__(name="capture", daemon=True)
        self.source = source
        self.buffer = buffer
        self.on_error = on_error
        self.loop = loop
        self.stop_event = threading.Event()
        self.meter = RateMeter()
        self.frames_read = 0

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                frame: Optional[Frame] = self.source.read()
                if frame is None:
                    if self.loop and hasattr(self.source, "rewind"):
                        self.source.rewind()  # type: ignore[attr-defined]
                        continue
                    break
                self.frames_read += 1
                self.meter.tick()
                if not self.buffer.put(frame, self.stop_event):
                    break
        except SourceError as exc:
            log.error("Capture error: %s", exc)
            self.on_error(str(exc))
        except Exception as exc:  # exception boundary for the thread
            log.exception("Unexpected capture failure")
            self.on_error(f"Unexpected capture failure: {exc}")
        finally:
            self.buffer.put(EOS, self.stop_event)
