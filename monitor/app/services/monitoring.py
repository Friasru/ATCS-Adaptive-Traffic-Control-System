"""Monitoring engine: capture -> detect/track -> count -> annotate -> record/store.

The engine is GUI-independent. It runs a processing thread fed by a capture
thread through a bounded FrameBuffer. The GUI (or a script) polls
``latest_snapshot()`` and receives state/error callbacks, which may be invoked
from worker threads.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import numpy as np

from app import paths
from app.camera.manager import EOS, CaptureWorker, FrameBuffer, RateMeter
from app.camera.sources import CameraSource, FrameSource, SourceError, VideoFileSource
from app.detection.classes import zero_counts
from app.detection.detector import Detector, ModelError
from app.recording.recorder import RecordingError, RecordingResult, VideoRecorder
from app.services.annotation import annotate
from app.settings import Settings
from app.storage.repository import EventWriter, Repository, iso
from app.tracking.counter import CountingEngine, CountingLine

log = logging.getLogger(__name__)

MAX_CONSECUTIVE_INFERENCE_FAILURES = 10
CAMERA_RECONNECT_ATTEMPTS = 3


class State(str, Enum):
    IDLE = "Idle"
    INITIALIZING = "Initializing"
    DETECTING = "Detecting"
    RECORDING = "Recording"
    STOPPING = "Stopping"
    ERROR = "Error"


@dataclass
class Snapshot:
    frame: Optional[np.ndarray] = None
    frame_index: int = 0
    visible: dict = field(default_factory=zero_counts)
    unique: dict = field(default_factory=zero_counts)
    crossings: dict = field(default_factory=zero_counts)
    line_totals: dict = field(default_factory=dict)
    capture_fps: float = 0.0
    inference_fps: float = 0.0
    recording_fps: float = 0.0
    latency_ms: float = 0.0
    frames_processed: int = 0
    frames_dropped: int = 0
    recording_frames_dropped: int = 0
    active_tracks: int = 0
    session_id: str = ""
    source_label: str = ""
    source_kind: str = ""
    device: str = ""
    media_time: float = 0.0
    recording_path: str = ""


@dataclass
class SessionReport:
    session_id: str
    status: str
    error: Optional[str]
    stats: dict
    recordings: list


class MonitoringEngine:
    def __init__(
        self,
        settings: Settings,
        repo: Repository,
        on_state: Optional[Callable[[State, str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        on_recording_finished: Optional[Callable[[RecordingResult], None]] = None,
        on_session_finished: Optional[Callable[[SessionReport], None]] = None,
        detector: Optional[Detector] = None,
        recordings_dir=None,
    ):
        self.settings = settings.copy()
        self.repo = repo
        self.on_state = on_state or (lambda s, m: None)
        self.on_error = on_error or (lambda m: None)
        self.on_recording_finished = on_recording_finished or (lambda r: None)
        self.on_session_finished = on_session_finished or (lambda r: None)
        self.detector = detector
        self.recordings_dir = recordings_dir or paths.recordings_dir()
        self._state = State.IDLE
        self._lock = threading.RLock()
        self._snapshot = Snapshot()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._record_request: Optional[bool] = None
        self._reset_request = False
        self._pending_lines: Optional[list[CountingLine]] = None
        self._finalizers: list[threading.Thread] = []
        self.last_report: Optional[SessionReport] = None
        self.last_recording: Optional[RecordingResult] = None

    # --------------------------------------------------------------- public
    @property
    def state(self) -> State:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def latest_snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def start(self, source: FrameSource, loop: bool = False) -> bool:
        """Start monitoring a source. Returns False if already running."""
        with self._lock:
            if self.is_running:
                return False
            self._stop.clear()
            self._record_request = None
            self._reset_request = False
            self._set_state(State.INITIALIZING, f"Opening {source.identifier}")
            self._thread = threading.Thread(target=self._run, args=(source, loop), name="pipeline", daemon=True)
            self._thread.start()
            return True

    def stop(self, wait: bool = False, timeout: float = 30.0) -> None:
        self._stop.set()
        if wait:
            self.join(timeout)

    def join(self, timeout: float = 30.0) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)
        for f in list(self._finalizers):
            f.join(timeout)

    def start_recording(self) -> None:
        self._record_request = True

    def stop_recording(self) -> None:
        self._record_request = False

    def reset_counters(self) -> None:
        """Close the current session (saving its counts) and continue in a new one."""
        self._reset_request = True

    def set_lines(self, lines: list[CountingLine], enabled: bool = True) -> None:
        for ln in lines:
            ln.validate()
        with self._lock:
            self.settings.lines_enabled = enabled
            self.settings.lines = [ln.to_dict() for ln in lines]
            self._pending_lines = [CountingLine(**{**ln.to_dict(), "enabled": ln.enabled and enabled}) for ln in lines]

    def update_live_settings(self, settings: Settings) -> None:
        """Apply settings that can change while running (conf, classes, lines...)."""
        settings.validate()
        with self._lock:
            self.settings = settings.copy()
            self._pending_lines = [CountingLine(**{**ln.to_dict(), "enabled": ln.enabled and settings.lines_enabled})
                                   for ln in settings.counting_lines()]

    # ------------------------------------------------------------- internals
    def _set_state(self, state: State, message: str = "") -> None:
        self._state = state
        try:
            self.on_state(state, message)
        except Exception:
            log.exception("on_state callback failed")

    def _report_error(self, message: str) -> None:
        log.error(message)
        try:
            self.on_error(message)
        except Exception:
            log.exception("on_error callback failed")

    def _ensure_detector(self, s: Settings) -> Detector:
        d = self.detector
        if d is None or d.model is None or d.model_name != s.model_name or d.device_pref != s.device:
            d = Detector(s.model_name, s.device, s.tracker)
            d.load()
            self.detector = d
        if d.tracker != s.tracker:
            d.tracker = s.tracker
        d.reset_tracker()
        return d

    def _new_counter(self, s: Settings) -> CountingEngine:
        lines = [CountingLine(**{**ln.to_dict(), "enabled": ln.enabled and s.lines_enabled}) for ln in s.counting_lines()]
        return CountingEngine(lines, min_hits=s.min_hits, hysteresis_frac=s.hysteresis_frac, anchor=s.anchor,
                              enabled_classes=s.enabled_classes, min_travel_frac=s.min_travel_frac)

    def _run(self, source: FrameSource, loop: bool) -> None:
        capture: Optional[CaptureWorker] = None
        writer: Optional[EventWriter] = None
        recorder: Optional[VideoRecorder] = None
        rec_id: Optional[int] = None
        session_id = ""
        counter: Optional[CountingEngine] = None
        session_t0, processed, infer_time, recording_seconds = time.monotonic(), 0, 0.0, 0.0
        status, error = "completed", None
        capture_error: list[str] = []
        try:
            with self._lock:
                s = self.settings.copy()
            try:
                detector = self._ensure_detector(s)
            except ModelError as exc:
                raise _Fatal(str(exc))
            try:
                source.open()
            except SourceError as exc:
                raise _Fatal(str(exc))

            counter = self._new_counter(s)
            session_id = self._open_session(source, detector, s)
            writer = EventWriter(self.repo)
            writer.start()
            session_t0 = time.monotonic()
            buffer = FrameBuffer(lossless=source.lossless)
            capture = CaptureWorker(source, buffer, capture_error.append, loop=loop)
            capture.start()
            infer_meter, rec_meter = RateMeter(), RateMeter()
            consecutive_failures = 0
            imgsz = s.effective_imgsz(detector.device)
            min_interval = 1.0 / s.max_processing_fps if s.max_processing_fps > 0 else 0.0
            last_proc = 0.0
            reconnects = 0
            self._set_state(State.DETECTING, f"Detecting on {source.identifier} ({detector.device}, {imgsz} px)")

            while not self._stop.is_set():
                # ---- requests from the UI ---------------------------------
                with self._lock:
                    pending_lines, self._pending_lines = self._pending_lines, None
                    live = self.settings
                if pending_lines is not None:
                    counter.set_lines(pending_lines)
                counter.set_enabled_classes(live.enabled_classes)

                if self._reset_request:
                    self._reset_request = False
                    if recorder is not None:
                        recording_seconds += self._finish_recording(recorder, rec_id)
                        recorder, rec_id = None, None
                        self._record_request = True  # keep recording in the new session
                    self._close_session(session_id, counter, writer, session_t0, processed, infer_time, capture,
                                        recording_seconds, "completed", None)
                    detector.reset_tracker()
                    counter = self._new_counter(live)
                    session_id = self._open_session(source, detector, live)
                    writer = EventWriter(self.repo)
                    writer.start()
                    session_t0, processed, infer_time, recording_seconds = time.monotonic(), 0, 0.0, 0.0
                    capture.frames_read = 0

                item = buffer.get(timeout=0.2)
                if item is None:
                    continue
                if item is EOS:
                    if capture_error:
                        if isinstance(source, CameraSource) and reconnects < CAMERA_RECONNECT_ATTEMPTS:
                            reconnects += 1
                            self._set_state(self._state, f"Camera lost - reconnecting ({reconnects}/{CAMERA_RECONNECT_ATTEMPTS})")
                            if self._reconnect(source):
                                capture_error.clear()
                                buffer = FrameBuffer(lossless=source.lossless)
                                capture = CaptureWorker(source, buffer, capture_error.append, loop=loop)
                                capture.start()
                                self._set_state(State.RECORDING if recorder else State.DETECTING, "Camera reconnected")
                                continue
                        status, error = "error", capture_error[-1]
                    break

                frame = item
                now = time.monotonic()
                if min_interval and not source.lossless and now - last_proc < min_interval:
                    continue  # rate limit for live sources: newer frames will follow
                last_proc = now

                # ---- recording requests (need the frame size) -------------
                req = self._record_request
                if req is True and recorder is None:
                    self._record_request = None
                    recorder, rec_id = self._begin_recording(session_id, source, s, frame.image)
                elif req is False:
                    self._record_request = None
                    if recorder is not None:
                        recording_seconds += self._finish_recording(recorder, rec_id)
                        recorder, rec_id = None, None
                        self._set_state(State.DETECTING, "Recording saved")

                # ---- inference ------------------------------------------------
                t0 = time.perf_counter()
                try:
                    dets = detector.track(frame.image, conf=live.confidence, iou=live.iou, imgsz=imgsz,
                                          classes=live.enabled_classes)
                    consecutive_failures = 0
                except Exception as exc:
                    consecutive_failures += 1
                    log.warning("Inference failed (%d in a row): %s", consecutive_failures, exc)
                    if consecutive_failures >= MAX_CONSECUTIVE_INFERENCE_FAILURES:
                        status, error = "error", f"Inference keeps failing: {exc}"
                        break
                    continue
                dt = time.perf_counter() - t0
                infer_time += dt
                processed += 1
                infer_meter.tick()

                h, w = frame.image.shape[:2]
                upd = counter.update(dets, (w, h), frame.wall_time)
                self._log_events(writer, session_id, upd, dets, live.event_log_level, frame.wall_time)

                annotated = annotate(frame.image, dets, counter.lines.values(), counter.line_totals, upd.visible,
                                     upd.crossings, recorder is not None, frame.wall_time, overlay=live.recording_overlay)
                rec_dropped = 0
                if recorder is not None:
                    if recorder.error:
                        self._report_error(recorder.error)
                        recording_seconds += self._finish_recording(recorder, rec_id)
                        recorder, rec_id = None, None
                        self._set_state(State.DETECTING, "Recording stopped because of an error")
                    else:
                        recorder.push(annotated, frame.media_time)
                        rec_meter.tick()
                        rec_dropped = recorder.result.frames_dropped if recorder.result else 0

                with self._lock:
                    self._snapshot = Snapshot(
                        frame=annotated, frame_index=frame.index, visible=upd.visible, unique=upd.unique,
                        crossings=upd.crossings,
                        line_totals={k: {d: dict(v) for d, v in t.items()} for k, t in counter.line_totals.items()},
                        capture_fps=capture.meter.rate, inference_fps=infer_meter.rate,
                        recording_fps=rec_meter.rate if recorder else 0.0, latency_ms=dt * 1000.0,
                        frames_processed=processed, frames_dropped=buffer.dropped,
                        recording_frames_dropped=rec_dropped, active_tracks=counter.active_track_count,
                        session_id=session_id, source_label=source.identifier, source_kind=source.kind,
                        device=detector.device, media_time=frame.media_time,
                        recording_path=recorder.result.path if recorder and recorder.result else "",
                    )

            self._set_state(State.STOPPING, "Stopping")
            if recorder is not None:
                recording_seconds += self._finish_recording(recorder, rec_id)
                recorder = None
            if capture is not None:
                capture.stop()
                capture.join(5)
            source.close()
            self._close_session(session_id, counter, writer, session_t0, processed, infer_time, capture,
                                recording_seconds, status, error)
            writer = None
            if error:
                self._report_error(error)
                self._set_state(State.ERROR, error)
            else:
                msg = "End of video" if isinstance(source, VideoFileSource) and not self._stop.is_set() else "Stopped"
                self._set_state(State.IDLE, msg)
        except _Fatal as exc:
            self._cleanup(capture, source, recorder, rec_id, writer)
            if session_id:
                self._mark_session(session_id, "error", str(exc))
            self._report_error(str(exc))
            self._set_state(State.ERROR, str(exc))
        except Exception as exc:  # exception boundary for the pipeline thread
            log.exception("Pipeline crashed")
            message = f"Internal error: {exc}"
            self._cleanup(capture, source, recorder, rec_id, None)
            saved = False
            if session_id and counter is not None:
                try:  # keep what was counted so far
                    self._close_session(session_id, counter, writer, session_t0, processed, infer_time, capture,
                                        recording_seconds, "error", message)
                    saved = True
                except Exception:
                    log.exception("Could not save session after crash")
            if not saved:
                if writer is not None:
                    writer.flush_and_stop()
                if session_id:
                    self._mark_session(session_id, "error", message)
            self._report_error(message)
            self._set_state(State.ERROR, message)

    def _reconnect(self, source: CameraSource) -> bool:
        source.close()
        for _ in range(5):
            if self._stop.wait(1.0):
                return False
            try:
                source.open()
                return True
            except SourceError as exc:
                log.info("Reconnect failed: %s", exc)
        return False

    def _cleanup(self, capture, source, recorder, rec_id, writer) -> None:
        try:
            if recorder is not None:
                self._finish_recording(recorder, rec_id)
        except Exception:
            log.exception("Recorder cleanup failed")
        if capture is not None:
            capture.stop()
            capture.join(5)
        try:
            source.close()
        except Exception:
            log.exception("Source close failed")
        if writer is not None:
            writer.flush_and_stop()

    def _open_session(self, source: FrameSource, detector: Detector, s: Settings) -> str:
        session_id = uuid.uuid4().hex
        source_id = source.identifier.split(":", 1)[1] if ":" in source.identifier else source.identifier
        self.repo.create_session(session_id, source.kind, source_id, s.model_name, detector.version,
                                 detector.device, s.to_dict(), started_at=iso())
        log.info("Session %s started on %s", session_id, source.identifier)
        return session_id

    def _mark_session(self, session_id: str, status: str, error: Optional[str]) -> None:
        try:
            self.repo.finish_session(session_id, status, {"unique": zero_counts(), "crossings": zero_counts()}, error)
        except Exception:
            log.exception("Could not mark session %s", session_id)

    def _close_session(self, session_id, counter: CountingEngine, writer: Optional[EventWriter], t0, processed,
                       infer_time, capture: Optional[CaptureWorker], recording_seconds, status, error) -> None:
        now = time.time()
        for ev in counter.finalize(now):
            self._log_track_event(writer, session_id, ev)
        if writer is not None:
            writer.flush_and_stop()
            if writer.failed:
                error = (error + "; " if error else "") + f"{writer.failed} events could not be saved"
                status = "error"
        duration = time.monotonic() - t0
        stats = {
            "unique": dict(counter.unique),
            "crossings": dict(counter.crossings),
            "line_totals": counter.line_totals,
            "duration_s": round(duration, 3),
            "frames_processed": processed,
            "avg_inference_fps": round(processed / infer_time, 2) if infer_time > 0 else None,
            "avg_capture_fps": round(capture.frames_read / duration, 2) if capture and duration > 0 else None,
            "peak_visible": counter.peak_visible_total,
            "avg_visible": round(counter.average_visible_total, 3),
            "recording_duration_s": round(recording_seconds, 3),
        }
        self.repo.finish_session(session_id, status, stats, error)
        report = SessionReport(session_id, status, error, stats, [])
        self.last_report = report
        log.info("Session %s finished (%s): crossings=%s unique=%s", session_id, status, stats["crossings"], stats["unique"])
        try:
            self.on_session_finished(report)
        except Exception:
            log.exception("on_session_finished callback failed")

    def _begin_recording(self, session_id: str, source: FrameSource, s: Settings, image: np.ndarray):
        h, w = image.shape[:2]
        if source.lossless and source.fps > 0:
            fps = source.fps
        elif s.max_processing_fps > 0:
            fps = s.max_processing_fps
        else:
            fps = source.fps or 15.0
        recorder = VideoRecorder(self.recordings_dir, session_id, fps, (w, h))
        try:
            res = recorder.start()
        except RecordingError as exc:
            self._report_error(f"Cannot start recording: {exc}")
            return None, None
        rec_id = self.repo.add_recording(session_id, res.path, res.codec, res.container, iso())
        self._set_state(State.RECORDING, f"Recording to {Path(res.path).name}")
        return recorder, rec_id

    def _finish_recording(self, recorder: VideoRecorder, rec_id: Optional[int]) -> float:
        """Stop the recorder now; validate + persist on a helper thread so the
        pipeline is not blocked by decoding the whole file."""
        res = recorder.stop(validate=False)

        def finalize():
            from app.recording.recorder import validate_video

            res.validation = validate_video(res.path, expected_frames=res.frames_written)
            res.ok = res.error is None and bool(res.validation.get("ok"))
            if res.error is None and not res.ok:
                res.error = "Recording file failed validation (not decodable)" if res.frames_written else "No frames were recorded"
            if rec_id is not None:
                try:
                    self.repo.finish_recording(rec_id, res.to_dict())
                except Exception:
                    log.exception("Could not store recording result")
            self.last_recording = res
            if not res.ok:
                self._report_error(f"Recording failed: {res.error}")
            try:
                self.on_recording_finished(res)
            except Exception:
                log.exception("on_recording_finished callback failed")

        t = threading.Thread(target=finalize, name="rec-finalize", daemon=False)
        self._finalizers = [f for f in self._finalizers if f.is_alive()] + [t]
        t.start()
        return res.duration_s

    def _log_events(self, writer: EventWriter, session_id: str, upd, dets, level: str, ts: float) -> None:
        stamp = iso(ts)
        for ev in upd.crossing_events:
            writer.add_crossing((session_id, iso(ev.timestamp), ev.line_id, ev.track_id, ev.cls, ev.direction))
        if level == "none":
            return
        for ev in upd.track_events:
            self._log_track_event(writer, session_id, ev)
        if level == "all":
            for d in dets:
                writer.add_detection((session_id, stamp, "detection", d.cls, round(d.confidence, 4), d.track_id,
                                      round(d.x1, 1), round(d.y1, 1), round(d.x2, 1), round(d.y2, 1)))

    def _log_track_event(self, writer: Optional[EventWriter], session_id: str, ev) -> None:
        if writer is None or self.settings.event_log_level == "none":
            return
        x1, y1, x2, y2 = ev.bbox
        writer.add_detection((session_id, iso(ev.timestamp), ev.event_type, ev.cls, round(ev.confidence, 4),
                              ev.track_id, round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)))


class _Fatal(Exception):
    pass
