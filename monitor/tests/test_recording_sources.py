"""Recorder lifecycle/validation and frame sources (synthetic video files)."""

import sys
from datetime import datetime

import cv2
import numpy as np
import pytest

from app.camera import sources
from app.camera.sources import (CameraInfo, CameraSource, SourceError, VideoFileSource, backend_name, camera_backends,
                                discover_cameras)
from app.recording import recorder as rec_mod
from app.recording.recorder import RecordingError, VideoRecorder, unique_recording_path, validate_video


def frame(i, w=320, h=240):
    img = np.full((h, w, 3), (i * 7) % 255, np.uint8)
    cv2.putText(img, str(i), (10, 100), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    return img


def write_video(path, n=30, fps=10, size=(320, 240)):
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(n):
        w.write(frame(i, *size))
    w.release()
    return path


# --------------------------------------------------------------- recorder
def test_unique_recording_names_never_overwrite(tmp_path):
    now = datetime(2026, 10, 23, 12, 0, 0)
    a = unique_recording_path(tmp_path, "abcdef1234", ".mp4", now)
    a.write_bytes(b"existing user data")
    b = unique_recording_path(tmp_path, "abcdef1234", ".mp4", now)
    assert a.name == "atcs_20261023-120000_abcdef12.mp4"
    assert b != a and b.name.endswith("_1.mp4")
    assert a.read_bytes() == b"existing user data"


def test_record_and_validate(tmp_path):
    r = VideoRecorder(tmp_path, "sess1234", 10.0, (320, 240))
    r.start()
    for i in range(25):
        assert r.push(frame(i), i / 10.0)
    res = r.stop()
    assert res.ok, res.error
    assert res.frames_written == 25 and res.frames_dropped == 0
    v = res.validation
    assert v["decoded_frames"] == 25 and v["first_frame_ok"] and v["last_frame_ok"]
    assert v["duration_s"] == pytest.approx(2.5, abs=0.11)
    assert res.container == "mp4" and res.codec == "mp4v"


def test_slow_pipeline_keeps_duration_by_duplicating(tmp_path):
    r = VideoRecorder(tmp_path, "slow", 10.0, (320, 240))
    r.start()
    for i in range(5):
        r.push(frame(i), i * 0.5)  # 2 fps input into a 10 fps file
    res = r.stop()
    assert res.ok
    assert res.frames_written == 21  # last input at t=2.0 s -> frames 0..20 at 10 fps
    assert res.frames_duplicated == 16
    assert res.validation["duration_s"] == pytest.approx(2.1)


def test_fast_input_skips_surplus(tmp_path):
    r = VideoRecorder(tmp_path, "fast", 5.0, (320, 240))
    r.start()
    for i in range(20):
        r.push(frame(i), i * 0.05)  # 20 fps input into 5 fps file
    res = r.stop()
    assert res.ok and res.frames_skipped > 0
    assert res.frames_written == 6  # t=0..0.95 s -> output frames 0..5 at 5 fps (rounded)


def test_frames_resized_to_recording_size(tmp_path):
    r = VideoRecorder(tmp_path, "resize", 10.0, (320, 240))
    r.start()
    r.push(frame(0, 640, 480), 0.0)
    r.push(frame(1, 640, 480), 0.1)
    assert r.stop().ok


def test_codec_fallback_to_mjpg(tmp_path):
    r = VideoRecorder(tmp_path, "fallback", 10.0, (320, 240), codecs=(("XXXX", ".mp4"), ("MJPG", ".avi")))
    r.start()
    for i in range(5):
        r.push(frame(i), i / 10)
    res = r.stop()
    assert res.ok and res.container == "avi" and res.codec == "MJPG"
    assert not list(tmp_path.glob("*.mp4"))  # failed placeholder cleaned up


def test_no_encoder_available(tmp_path):
    r = VideoRecorder(tmp_path, "none", 10.0, (320, 240), codecs=(("XXXX", ".mp4"),))
    with pytest.raises(RecordingError, match="No working video encoder"):
        r.start()


def test_empty_recording_is_reported_as_failure(tmp_path):
    r = VideoRecorder(tmp_path, "empty", 10.0, (320, 240))
    r.start()
    res = r.stop()
    assert not res.ok and res.error


def test_insufficient_disk_space(tmp_path, monkeypatch):
    monkeypatch.setattr(rec_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 10 * 1024 * 1024})())
    with pytest.raises(RecordingError, match="disk space"):
        VideoRecorder(tmp_path, "full", 10.0, (320, 240)).start()


def test_disk_failure_while_recording_is_reported(tmp_path, monkeypatch):
    r = VideoRecorder(tmp_path, "diskfail", 10.0, (320, 240))
    r.start()
    monkeypatch.setattr(rec_mod.shutil, "disk_usage", lambda p: type("U", (), {"free": 1024})())
    for i in range(150):
        r.push(frame(i), i / 10)
        if r.error:
            break
    import time

    for _ in range(50):
        if r.error:
            break
        time.sleep(0.02)
    res = r.stop()
    assert not res.ok and "Disk almost full" in res.error


@pytest.mark.parametrize("fps,size", [(0, (320, 240)), (10, (0, 240)), (500, (10, 10))])
def test_invalid_recorder_args(tmp_path, fps, size):
    with pytest.raises(RecordingError):
        VideoRecorder(tmp_path, "x", fps, size)


def test_validate_video_on_garbage(tmp_path):
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"\x00" * 1000)
    assert validate_video(p)["ok"] is False
    assert validate_video(tmp_path / "missing.mp4")["exists"] is False


# ----------------------------------------------------------------- sources
def test_video_file_source_reads_all_frames_and_ends(tmp_path):
    src = VideoFileSource(write_video(tmp_path / "v.mp4", n=12, fps=10), realtime=False)
    src.open()
    frames = []
    while (f := src.read()) is not None:
        frames.append(f)
    assert len(frames) == 12
    assert frames[-1].media_time == pytest.approx(1.1)
    assert src.fps == pytest.approx(10)
    src.rewind()
    assert src.read().index == 0
    src.close()
    assert not src.is_open


def test_video_file_source_errors(tmp_path):
    with pytest.raises(SourceError, match="not found"):
        VideoFileSource(tmp_path / "nope.mp4").open()
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video at all" * 100)
    with pytest.raises(SourceError, match="decode|decodable"):
        VideoFileSource(bad).open()


def test_invalid_camera_index():
    with pytest.raises(SourceError):
        CameraSource(-1)
    with pytest.raises(SourceError, match="does not exist|could not be opened"):
        CameraSource(97).open()


def test_discover_cameras_returns_list():
    cams = discover_cameras()
    assert isinstance(cams, list)  # empty when no camera is attached (e.g. WSL2)


def test_camera_backend_order_per_platform(monkeypatch):
    monkeypatch.delenv("ATCS_CAMERA_BACKEND", raising=False)
    monkeypatch.setattr(sources, "_has_backend", lambda api: True)
    for plat, expected in (("linux", [cv2.CAP_V4L2]), ("win32", [cv2.CAP_DSHOW, cv2.CAP_MSMF]),
                           ("darwin", [cv2.CAP_AVFOUNDATION]), ("freebsd14", [cv2.CAP_ANY])):
        monkeypatch.setattr(sys, "platform", plat)
        assert camera_backends() == expected


def test_camera_backend_override(monkeypatch):
    monkeypatch.setenv("ATCS_CAMERA_BACKEND", "MSMF")
    assert camera_backends() == [cv2.CAP_MSMF]
    monkeypatch.setenv("ATCS_CAMERA_BACKEND", "bogus")  # ignored with a warning
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sources, "_has_backend", lambda api: True)
    assert camera_backends() == [cv2.CAP_DSHOW, cv2.CAP_MSMF]
    assert backend_name(cv2.CAP_DSHOW) == "dshow"


def test_windows_skips_backends_missing_from_the_opencv_build(monkeypatch):
    monkeypatch.delenv("ATCS_CAMERA_BACKEND", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sources, "_has_backend", lambda api: api != cv2.CAP_MSMF)
    assert camera_backends() == [cv2.CAP_DSHOW]
    monkeypatch.setattr(sources, "_has_backend", lambda api: False)
    assert camera_backends() == [cv2.CAP_ANY]


def test_windows_discovery_uses_directshow_names_then_msmf_fallback(monkeypatch):
    """Simulated Windows: DirectShow sees nothing, Media Foundation finds index 0."""
    monkeypatch.delenv("ATCS_CAMERA_BACKEND", raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sources, "_has_backend", lambda api: True)
    monkeypatch.setattr(sources, "_windows_camera_names", lambda: ["Integrated Camera"])
    probed = []

    def fake_probe(idx, api):
        probed.append((idx, api))
        return api == cv2.CAP_MSMF and idx == 0

    monkeypatch.setattr(sources, "_probe", fake_probe)
    cams = discover_cameras(max_index=3)
    assert cams == [CameraInfo(0, "Integrated Camera", cv2.CAP_MSMF)]
    assert probed == [(0, cv2.CAP_DSHOW), (0, cv2.CAP_MSMF), (1, cv2.CAP_MSMF), (2, cv2.CAP_MSMF)]


def test_camera_unknown_fps_is_reported_as_zero(monkeypatch):
    """DirectShow returns CAP_PROP_FPS == -1; that must not reach the recorder as a frame rate."""

    class FakeCap:
        def __init__(self, *a):
            pass

        def isOpened(self):
            return True

        def set(self, *a):
            return True

        def read(self):
            return True, np.zeros((48, 64, 3), np.uint8)

        def get(self, prop):
            return -1.0

        def release(self):
            pass

    monkeypatch.setattr(sources.cv2, "VideoCapture", FakeCap)
    monkeypatch.setattr(sys, "platform", "win32")
    src = CameraSource(0, backend=cv2.CAP_DSHOW)
    src.open()
    assert src.fps == 0.0 and src.read() is not None
    src.close()
