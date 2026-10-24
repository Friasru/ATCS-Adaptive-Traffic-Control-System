"""MonitoringEngine integration tests with a scripted detector.

A deterministic stand-in detector is used here to exercise lifecycle and
failure handling (inference errors, disconnects, rapid start/stop). Real YOLO
inference is covered in test_real_pipeline.py.
"""

import time

import cv2
import numpy as np
import pytest

from app.camera.sources import Frame, FrameSource, SourceError, VideoFileSource
from app.detection.types import Detection
from app.services.monitoring import MonitoringEngine, State
from app.settings import Settings
from app.storage.repository import Repository


class ScriptedDetector:
    """Moves one 'car' (track 1) from top to bottom: frame k -> y = 10 + 8k."""

    def __init__(self, fail_on=(), fail_always=False):
        self.model = object()
        self.model_name = "yolo26n.pt"
        self.device_pref = "auto"
        self.tracker = "bytetrack"
        self.device = "cpu"
        self.version = "scripted"
        self.calls = 0
        self.resets = 0
        self.fail_on = set(fail_on)
        self.fail_always = fail_always

    def reset_tracker(self):
        self.resets += 1
        self.calls = 0

    def track(self, image, conf, iou, imgsz, classes=None):
        k = self.calls
        self.calls += 1
        if self.fail_always or k in self.fail_on:
            raise RuntimeError("simulated inference failure")
        y = 10 + 8 * k
        h, w = image.shape[:2]
        if y + 20 > h:
            return []
        return [Detection("car", 0.9, w / 2 - 15, y, w / 2 + 15, y + 20, 1)]


def make_video(path, n=30, fps=10, size=(160, 240)):
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(n):
        wr.write(np.full((size[1], size[0], 3), 40 + i % 200, np.uint8))
    wr.release()
    return path


def settings(**kw):
    s = Settings(max_processing_fps=0, min_hits=2, **kw)
    s.lines = [{"line_id": "line-1", "p1": [0.0, 0.5], "p2": [1.0, 0.5], "direction": "both", "enabled": True}]
    s.validate()
    return s


def wait(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "e.sqlite3")
    yield r
    r.close()


def engine(repo, tmp_path, det=None, **kw):
    errors, states = [], []
    eng = MonitoringEngine(settings(**kw), repo, on_state=lambda s, m: states.append(s), on_error=errors.append,
                           detector=det or ScriptedDetector(), recordings_dir=tmp_path / "rec")
    return eng, errors, states


def test_video_end_to_end_counts_records_and_stores(repo, tmp_path):
    eng, errors, states = engine(repo, tmp_path)
    assert eng.start(VideoFileSource(make_video(tmp_path / "v.mp4"), realtime=False))
    eng.start_recording()
    eng.join()
    assert errors == []
    assert states[0] == State.INITIALIZING and states[-1] == State.IDLE
    assert State.RECORDING in states
    rep = eng.last_report
    assert rep.status == "completed"
    assert rep.stats["frames_processed"] == 30  # lossless: every frame of the file
    assert rep.stats["crossings"]["car"] == 1 and rep.stats["unique"]["car"] == 1
    s = repo.get_session(rep.session_id)
    assert s["status"] == "completed"
    events = repo.crossing_events(rep.session_id)
    assert [(e["cls"], e["direction"], e["track_id"]) for e in events] == [("car", "b_to_a", 1)]
    rec = s["recordings"][0]
    assert rec["ok"] and rec["info"]["validation"]["decoded_frames"] == rec["frames"]
    assert eng.last_recording.validation["duration_s"] == pytest.approx(3.0, abs=0.15)


def test_temporary_inference_failure_is_survived(repo, tmp_path):
    det = ScriptedDetector(fail_on={3, 4})
    eng, errors, _ = engine(repo, tmp_path, det)
    eng.start(VideoFileSource(make_video(tmp_path / "v.mp4"), realtime=False))
    eng.join()
    assert eng.last_report.status == "completed" and errors == []
    assert eng.last_report.stats["frames_processed"] == 28


def test_persistent_inference_failure_ends_in_error(repo, tmp_path):
    eng, errors, states = engine(repo, tmp_path, ScriptedDetector(fail_always=True))
    eng.start(VideoFileSource(make_video(tmp_path / "v.mp4"), realtime=False))
    eng.join()
    assert states[-1] == State.ERROR
    assert "Inference keeps failing" in errors[-1]
    assert repo.get_session(eng.last_report.session_id)["status"] == "error"


def test_unsupported_file_reports_error_without_session(repo, tmp_path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"garbage" * 500)
    eng, errors, states = engine(repo, tmp_path)
    eng.start(VideoFileSource(bad))
    eng.join()
    assert states[-1] == State.ERROR and "decode" in errors[-1]
    assert repo.list_sessions() == []


class DisconnectingSource(FrameSource):
    """Behaves like a camera that is unplugged after N frames."""

    kind = "camera"
    lossless = False

    def __init__(self, n=15):
        self.n, self.i, self.opened, self.closed = n, 0, False, 0

    @property
    def identifier(self):
        return "camera:9:Fake USB camera"

    @property
    def fps(self):
        return 10.0

    def open(self):
        self.opened = True

    def read(self):
        if self.i >= self.n:
            raise SourceError("Camera #9 stopped delivering frames (disconnected?)")
        time.sleep(0.02)
        self.i += 1
        return Frame(np.zeros((240, 160, 3), np.uint8), self.i - 1, time.time(), self.i * 0.1)

    def close(self):
        self.closed += 1


def test_camera_disconnect_finalizes_recording_and_marks_error(repo, tmp_path):
    src = DisconnectingSource()
    eng, errors, states = engine(repo, tmp_path)
    eng.start(src)
    eng.start_recording()
    eng.join(30)
    assert states[-1] == State.ERROR
    assert "disconnected" in errors[-1]
    assert src.closed >= 1  # device released
    s = repo.get_session(eng.last_report.session_id)
    assert s["status"] == "error"
    assert s["recordings"] and s["recordings"][0]["ended_at"]  # recording finalized, not left open


def test_repeated_start_stop_cycles(repo, tmp_path):
    video = make_video(tmp_path / "v.mp4", n=200)
    eng, errors, _ = engine(repo, tmp_path)
    for _ in range(5):
        assert eng.start(VideoFileSource(video, realtime=True))
        assert not eng.start(VideoFileSource(video))  # second start while running is refused
        assert wait(lambda: eng.state == State.DETECTING)
        eng.stop(wait=True)
        assert not eng.is_running and eng.state == State.IDLE
    assert errors == []
    sessions = repo.list_sessions()
    assert len(sessions) == 5 and all(s["status"] == "completed" for s in sessions)


def test_reset_counters_rolls_to_new_session_and_keeps_recording(repo, tmp_path):
    video = make_video(tmp_path / "v.mp4", n=60)
    det = ScriptedDetector()
    eng, errors, _ = engine(repo, tmp_path, det)
    eng.start(VideoFileSource(video, realtime=True))
    assert wait(lambda: eng.state == State.DETECTING)
    eng.start_recording()
    assert wait(lambda: eng.latest_snapshot().crossings["car"] == 1)
    first = eng.latest_snapshot().session_id
    eng.reset_counters()
    assert wait(lambda: eng.latest_snapshot().session_id not in ("", first))
    snap = eng.latest_snapshot()
    assert snap.crossings["car"] == 0  # counters restarted
    eng.join()
    sessions = {s["session_id"]: s for s in repo.list_sessions()}
    assert len(sessions) == 2
    assert sessions[first]["crossings"]["car"] == 1  # saved before reset
    assert all(s["recording_paths"] for s in sessions.values())  # recording continued into the new session
    assert det.resets >= 2


def test_live_source_drops_stale_frames_and_reports_them(repo, tmp_path):
    class SlowDetector(ScriptedDetector):
        def track(self, *a, **k):
            time.sleep(0.08)
            return []

    class FastCam(DisconnectingSource):
        def read(self):
            if self.i >= self.n:
                return None
            time.sleep(0.005)
            self.i += 1
            return Frame(np.zeros((240, 160, 3), np.uint8), self.i - 1, time.time(), self.i * 0.005)

    eng, errors, _ = engine(repo, tmp_path, SlowDetector())
    eng.start(FastCam(n=200))
    eng.join()
    snap = eng.latest_snapshot()
    assert snap.frames_dropped > 0
    assert snap.frames_processed + snap.frames_dropped <= 200


def test_line_moved_while_running(repo, tmp_path):
    from app.tracking.counter import CountingLine

    eng, errors, _ = engine(repo, tmp_path)
    eng.start(VideoFileSource(make_video(tmp_path / "v.mp4", n=30), realtime=True))
    assert wait(lambda: eng.state == State.DETECTING)
    eng.set_lines([CountingLine("line-1", (0.0, 0.9), (1.0, 0.9))])
    eng.join()
    assert errors == []
    assert eng.last_report.stats["crossings"]["car"] <= 1


def test_stop_while_recording_finalizes(repo, tmp_path):
    eng, errors, _ = engine(repo, tmp_path)
    eng.start(VideoFileSource(make_video(tmp_path / "v.mp4", n=300), realtime=True))
    assert wait(lambda: eng.state == State.DETECTING)
    eng.start_recording()
    assert wait(lambda: eng.state == State.RECORDING)
    time.sleep(0.5)
    eng.stop(wait=True)
    assert eng.last_recording is not None and eng.last_recording.ok
    s = repo.get_session(eng.last_report.session_id)
    assert s["recordings"][0]["ok"]


def test_replayed_video_keeps_recording_timeline(repo, tmp_path):
    """With 'Replay video', media time must keep increasing so the recorder
    does not discard frames after the first loop."""
    eng, errors, _ = engine(repo, tmp_path)
    eng.start(VideoFileSource(make_video(tmp_path / "v.mp4", n=10, fps=10), realtime=True), loop=True)
    assert wait(lambda: eng.state == State.DETECTING)
    eng.start_recording()
    assert wait(lambda: eng.latest_snapshot().media_time > 2.5, timeout=30)
    eng.stop(wait=True)
    res = eng.last_recording
    assert res.ok, res.error
    assert res.frames_skipped <= 1
    assert res.frames_written >= 20  # more than two loops of a 10-frame file


def test_internal_crash_keeps_counts_already_made(repo, tmp_path):
    class CrashAfter(ScriptedDetector):
        def track(self, image, *a, **k):
            if self.calls >= 25:
                self.calls += 1
                return [object()]  # malformed result -> counting code raises
            return super().track(image, *a, **k)

    eng, errors, states = engine(repo, tmp_path, CrashAfter())
    eng.start(VideoFileSource(make_video(tmp_path / "v.mp4", n=30), realtime=False))
    eng.join()
    assert states[-1] == State.ERROR and errors[-1].startswith("Internal error")
    s = repo.get_session(eng.last_report.session_id)
    assert s["status"] == "error"
    assert s["stats"]["crossings"]["car"] == 1  # not wiped to zero
    assert len(repo.crossing_events(s["session_id"])) == 1
