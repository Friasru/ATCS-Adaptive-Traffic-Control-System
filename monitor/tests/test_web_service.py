"""Local monitor web service: authentication, allow-listed sources, lifecycle and shutdown.

Uses a scripted detector and generated videos, so no webcam, CUDA or YOLO weights
are needed.
"""

import http.client
import json
import socket
import subprocess
import sys
import threading
import time

import cv2
import numpy as np
import pytest

import web_service as ws
from app.camera.sources import FrameSource, SourceError
from app.detection.types import Detection
from app.services.monitoring import State
from app.settings import Settings
from app.storage.repository import Repository

SECRET = "test-service-secret-" + "x" * 32


class ScriptedDetector:
    """One 'car' and one 'person' moving down the frame; car crosses the middle line."""

    def __init__(self, fail_always=False):
        self.model = object()
        self.model_name = "yolo26n.pt"
        self.device_pref = "auto"
        self.tracker = "bytetrack"
        self.device = "cpu"
        self.version = "scripted"
        self.calls = 0
        self.fail_always = fail_always

    def reset_tracker(self):
        self.calls = 0

    def track(self, image, conf, iou, imgsz, classes=None):
        k = self.calls
        self.calls += 1
        if self.fail_always:
            raise RuntimeError("simulated inference failure")
        h, w = image.shape[:2]
        y = (10 + 8 * k) % (h - 30)
        return [Detection("car", 0.9, w / 2 - 15, y, w / 2 + 15, y + 20, 1),
                Detection("person", 0.9, 5, 5, 25, 45, 2)]


class FailingSource(FrameSource):
    kind = "camera"

    def __init__(self, message):
        self.message = message
        self.closed = False

    def open(self):
        raise SourceError(self.message)

    def read(self):
        return None

    def close(self):
        self.closed = True

    @property
    def identifier(self):
        return "camera:7:Fake"


def make_video(path, n=40, fps=20, size=(160, 240)):
    wr = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(n):
        wr.write(np.full((size[1], size[0], 3), 40 + i % 200, np.uint8))
    wr.release()
    return path


def wait(pred, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def settings():
    s = Settings(max_processing_fps=0, min_hits=2)
    s.lines = [{"line_id": "line-1", "p1": [0.0, 0.5], "p2": [1.0, 0.5], "direction": "both", "enabled": True}]
    s.validate()
    return s


@pytest.fixture
def videos(home):
    d = home / "videos"
    d.mkdir()
    make_video(d / "clip.mp4")
    return d


@pytest.fixture
def repo(home):
    r = ws.ServiceRepository(home / "svc.sqlite3")
    yield r
    r.close()


@pytest.fixture
def make_service(home, repo, videos):
    made = []

    def _make(detector="scripted", cameras=(7,)):
        det = ScriptedDetector() if detector == "scripted" else detector
        svc = ws.MonitorService(settings(), repo, ws.SourceCatalog(cameras, videos), detector=det,
                                recordings_dir=home / "rec")
        made.append(svc)
        return svc

    yield _make
    for svc in made:
        svc.close(timeout=10)


@pytest.fixture
def server(make_service):
    servers = []

    def _serve(service=None, secret=SECRET, **kw):
        service = service or make_service()
        srv = ws.MonitorHTTPServer(0, service, secret, **kw)
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        servers.append(srv)
        return srv

    yield _serve
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def call(srv, method, path, body=None, token: "str | None" = SECRET, headers=None, raw=None) -> tuple:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=15)
    h = dict(headers or {})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=h)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    ctype = resp.getheader("Content-Type", "")
    return resp.status, (json.loads(payload) if ctype == "application/json" else payload), resp


# ------------------------------------------------------------------ auth
def test_server_binds_to_localhost_only(server):
    srv = server()
    assert srv.server_address[0] == "127.0.0.1"
    assert ws.BIND_HOST == "127.0.0.1"


@pytest.mark.parametrize("secret", ["", None, "short", "x" * 31])
def test_weak_or_missing_secret_refuses_to_start(make_service, secret):
    with pytest.raises(ValueError):
        ws.MonitorHTTPServer(0, make_service(), secret)


@pytest.mark.parametrize("token", [None, "", "wrong-" + "y" * 40, SECRET + "x", SECRET[:-1]])
@pytest.mark.parametrize("method,path", [("GET", "/status"), ("GET", "/frame.jpg"), ("POST", "/start"),
                                         ("POST", "/stop"), ("GET", "/nope")])
def test_missing_or_wrong_token_is_rejected(server, token, method, path):
    srv = server()
    status, body, _ = call(srv, method, path, token=token, body={"source": "video:clip.mp4"} if method == "POST" else None)
    assert status == 401
    assert body == {"error": "missing or invalid service token", "code": "unauthorized"}
    assert not srv.service.engine.is_running


def test_non_bearer_scheme_is_rejected(server):
    srv = server()
    status, _, _ = call(srv, "GET", "/status", token=None, headers={"Authorization": f"Basic {SECRET}"})
    assert status == 401


def test_valid_token_status(server):
    status, body, resp = call(server(), "GET", "/status")
    assert status == 200
    assert body["running"] is False and body["state"] == "Idle"
    assert body["model"] == {"name": "yolo26n.pt", "available": True}
    assert resp.getheader("Cache-Control") == "no-store"


def test_token_never_logged(server, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        srv = server()
        call(srv, "GET", "/status")
        call(srv, "GET", "/status", token="wrong-" + "z" * 40)
    assert SECRET not in caplog.text and "zzzz" not in caplog.text


@pytest.mark.parametrize("method,path", [("POST", "/status"), ("GET", "/start"), ("DELETE", "/stop"),
                                         ("PUT", "/recording/start"), ("POST", "/frame.jpg")])
def test_wrong_method_is_405(server, method, path):
    status, body, resp = call(server(), method, path)
    assert status == 405 and body["code"] == "method_not_allowed"
    assert resp.getheader("Allow")


def test_concurrency_limit_returns_503(server, make_service):
    srv = server(max_concurrent=1)
    srv.slots.acquire()  # simulate one request in flight
    try:
        status, body, _ = call(srv, "GET", "/status")
    finally:
        srv.slots.release()
    assert status == 503 and body["code"] == "busy"


# --------------------------------------------------------------- sources
def test_sources_are_allow_listed_without_paths(server, videos, home):
    (videos / "notes.txt").write_text("x")
    (videos / "bad;name.mp4").write_bytes(b"x")
    outside = make_video(home / "outside.mp4")
    (videos / "link.mp4").symlink_to(outside)
    srv = server()
    status, body, _ = call(srv, "GET", "/sources")
    assert status == 200
    assert body["sources"] == [
        {"id": "camera:7", "kind": "camera", "label": "Camera 7"},
        {"id": "video:clip.mp4", "kind": "video_file", "label": "clip.mp4"},
    ]
    assert str(home) not in json.dumps(body)


@pytest.mark.parametrize("source", [
    "video:../outside.mp4", "video:/etc/passwd", "../../etc/passwd", "/tmp/clip.mp4", "file:clip.mp4",
    "video:link.mp4", "video:missing.mp4", "camera:3", "camera:-1", "camera:abc", "", None, 5, ["video:clip.mp4"],
])
def test_invalid_or_unknown_source_is_400(server, source, videos, home):
    (videos / "link.mp4").symlink_to(make_video(home / "outside.mp4"))
    srv = server()
    status, body, _ = call(srv, "POST", "/start", body={"source": source})
    assert status == 400 and body["code"] in ("invalid_source", "unknown_source")
    assert not srv.service.engine.is_running


@pytest.mark.parametrize("raw,ctype,code", [
    (b"not json", "application/json", 400),
    (b"[1, 2]", "application/json", 400),
    (b'{"source": "video:clip.mp4"}', "text/plain", 415),
    (b'{"source": "video:clip.mp4", "path": "/etc"}', "application/json", 400),
    (b'{"source": "video:clip.mp4", "loop": "yes"}', "application/json", 400),
    (b'{"source": "' + b"a" * 5000 + b'"}', "application/json", 413),
])
def test_invalid_bodies(server, raw, ctype, code):
    srv = server()
    status, body, _ = call(srv, "POST", "/start", raw=raw, headers={"Content-Type": ctype})
    assert status == code and "error" in body
    assert not srv.service.engine.is_running


def test_parse_helpers():
    assert ws.parse_cameras("0, 1 2") == [0, 1, 2]
    assert ws.parse_cameras("") == []
    for bad in ("-1", "x", "99"):
        with pytest.raises(ValueError):
            ws.parse_cameras(bad)
    assert ws.parse_port("8765") == 8765
    for bad in ("80", "70000", "abc", ""):
        with pytest.raises(ValueError):
            ws.parse_port(bad)


# ------------------------------------------------------------- lifecycle
def test_start_stop_transitions_and_counts(server):
    srv = server()
    status, body, _ = call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    assert status == 202 and body["running"] is True
    assert body["source"] == {"id": "video:clip.mp4", "kind": "video_file", "label": "clip.mp4"}
    eng = srv.service.engine
    assert wait(lambda: eng.state == State.DETECTING and eng.latest_snapshot().frames_processed > 25)
    _, st, _ = call(srv, "GET", "/status")
    assert st["state"] == "Detecting" and st["session_id"]
    assert st["visible"]["car"] == 1 and st["visible"]["person"] == 1
    assert st["totals"]["visible_vehicles"] == 1 and st["totals"]["visible_pedestrians"] == 1
    assert st["unique"]["car"] == 1 and st["totals"]["unique_vehicles"] == 1
    assert set(st["crossings"]) == {"person", "car", "motorcycle", "bicycle", "bus", "truck"}
    assert st["totals"]["crossing_vehicles"] == sum(st["crossings"][c] for c in ("car", "motorcycle", "bus", "truck"))
    assert st["performance"]["frames_processed"] > 0 and st["performance"]["inference_fps"] >= 0
    assert st["vehicle_classes"] == ["car", "motorcycle", "bus", "truck"]

    status, body, _ = call(srv, "POST", "/stop")
    assert status == 200 and body["running"] is False and body["state"] == "Idle"
    assert body["last_session"]["status"] == "completed"
    status, body, _ = call(srv, "POST", "/stop")
    assert status == 409 and body["code"] == "not_running"
    # A new session can start after a stop.
    status, _, _ = call(srv, "POST", "/start", body={"source": "video:clip.mp4"})
    assert status == 202


def test_duplicate_start_is_409(server):
    srv = server()
    assert call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})[0] == 202
    status, body, _ = call(srv, "POST", "/start", body={"source": "camera:7"})
    assert status == 409 and body["code"] == "already_running"


def test_concurrent_starts_create_one_session(server, repo):
    srv = server()
    results = []
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        results.append(call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})[0])

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [202] + [409] * 7
    assert wait(lambda: srv.service.engine.state == State.DETECTING)
    assert len(repo.list_sessions()) == 1


def test_recording_controls(server, home, repo):
    srv = server()
    assert call(srv, "POST", "/recording/start")[1]["code"] == "not_running"
    call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    eng = srv.service.engine
    assert wait(lambda: eng.state == State.DETECTING)
    assert call(srv, "POST", "/recording/stop")[1]["code"] == "not_recording"
    status, body, _ = call(srv, "POST", "/recording/start")
    assert status == 202 and body["recording_requested"] is True
    assert wait(lambda: eng.state == State.RECORDING)
    status, body, _ = call(srv, "POST", "/recording/start")
    assert status == 409 and body["code"] == "already_recording"
    assert wait(lambda: call(srv, "GET", "/status")[1]["recording"]["file"])
    st = call(srv, "GET", "/status")[1]
    assert st["recording"]["active"] is True
    assert st["recording"]["file"].startswith("atcs_") and "/" not in st["recording"]["file"]
    assert call(srv, "POST", "/recording/start", body={"x": 1})[0] == 400
    status, body, _ = call(srv, "POST", "/recording/stop")
    assert status == 202
    assert wait(lambda: eng.state == State.DETECTING)
    call(srv, "POST", "/stop")
    eng.join()
    rec = repo.get_session(eng.last_report.session_id)["recordings"][0]
    assert rec["ok"] and rec["ended_at"]
    assert list((home / "rec").iterdir())


def test_frame_jpeg_is_cached_per_frame(server):
    srv = server()
    status, body, _ = call(srv, "GET", "/frame.jpg")
    assert status == 404 and body["code"] == "no_frame"
    call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    assert wait(lambda: srv.service.engine.latest_snapshot().frame is not None
                and srv.service.engine.latest_snapshot().session_id)
    status, jpeg, resp = call(srv, "GET", "/frame.jpg")
    assert status == 200 and resp.getheader("Content-Type") == "image/jpeg"
    assert jpeg[:2] == b"\xff\xd8" and jpeg[-2:] == b"\xff\xd9"
    assert resp.getheader("Cache-Control") == "no-store"
    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[:2] == (240, 160)
    call(srv, "POST", "/stop")
    # Stopped: the last frame stays; many viewers share one encoding.
    before = srv.service.jpeg_encodes
    for _ in range(10):
        assert call(srv, "GET", "/frame.jpg")[0] == 200
    assert srv.service.jpeg_encodes - before <= 1


def test_old_frame_and_counts_hidden_after_restart(server):
    srv = server()
    call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    assert wait(lambda: srv.service.engine.latest_snapshot().frames_processed > 3)
    call(srv, "POST", "/stop")
    svc = srv.service
    svc.catalog.build_source = lambda entry: FailingSource("Camera #7 could not be opened")
    call(srv, "POST", "/start", body={"source": "camera:7"})
    assert wait(lambda: svc.engine.state == State.ERROR)
    st = call(srv, "GET", "/status")[1]
    assert st["session_id"] is None and st["visible"]["car"] == 0
    assert call(srv, "GET", "/frame.jpg")[0] == 404


# ------------------------------------------------------------- failures
def test_unavailable_camera_reports_error_and_recovers(server):
    srv = server()
    fake = FailingSource("Camera #7 could not be opened (in use by another program or no permission?)")
    srv.service.catalog.build_source = lambda entry: fake
    status, _, _ = call(srv, "POST", "/start", body={"source": "camera:7"})
    assert status == 202
    eng = srv.service.engine
    assert wait(lambda: eng.state == State.ERROR and not eng.is_running)
    st = call(srv, "GET", "/status")[1]
    assert st["running"] is False and st["state"] == "Error"
    assert "Camera #7 could not be opened" in st["last_error"]
    assert fake.closed
    srv.service.catalog.build_source = ws.SourceCatalog.build_source.__get__(srv.service.catalog)
    assert call(srv, "POST", "/start", body={"source": "video:clip.mp4"})[0] == 202
    assert call(srv, "GET", "/status")[1]["last_error"] is None


def test_inference_failure_ends_in_error(server, make_service):
    srv = server(make_service(ScriptedDetector(fail_always=True)))
    call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    eng = srv.service.engine
    assert wait(lambda: eng.state == State.ERROR and not eng.is_running)
    st = call(srv, "GET", "/status")[1]
    assert "Inference keeps failing" in st["last_error"]
    assert st["last_session"]["status"] == "error"


def test_missing_model_is_503(server, make_service):
    srv = server(make_service(detector=None))  # real Detector, no weights in the isolated home
    status, body, _ = call(srv, "POST", "/start", body={"source": "video:clip.mp4"})
    assert status == 503 and body["code"] == "model_unavailable"
    assert "/" not in body["error"]
    assert call(srv, "GET", "/status")[1]["model"]["available"] is False
    assert not srv.service.engine.is_running


def test_internal_error_is_generic_500(server, monkeypatch, home):
    srv = server()
    monkeypatch.setattr(srv.service, "status", lambda: (_ for _ in ()).throw(OSError(f"boom {home}/secret.db")))
    status, body, _ = call(srv, "GET", "/status")
    assert status == 500 and body == {"error": "internal monitor service error", "code": "internal_error"}


def test_paths_are_redacted(server, home):
    srv = server()
    leaked = f"Video file not found: {home}/videos/clip.mp4 and C:\\Users\\alberto\\Videos\\x.mp4"
    srv.service.catalog.build_source = lambda entry: FailingSource(leaked)
    call(srv, "POST", "/start", body={"source": "camera:7"})
    assert wait(lambda: srv.service.engine.state == State.ERROR)
    text = json.dumps(call(srv, "GET", "/status")[1]) + json.dumps(call(srv, "GET", "/sessions")[1])
    assert str(home) not in text and "Users" not in text and "alberto" not in text
    assert "clip.mp4" in text and "x.mp4" in text


@pytest.mark.parametrize("msg,expected", [
    ("Model weights not found: /home/u/x/models/yolo26n.pt. Run 'scripts/download_model.py'",
     "Model weights not found: yolo26n.pt. Run 'scripts/download_model.py'"),
    ("Camera lost - reconnecting (1/3)", "Camera lost - reconnecting (1/3)"),
    ("[Errno 2] No such file: 'D:\\data\\a b\\rec.mp4'", "[Errno 2] No such file: 'rec.mp4'"),
    (None, None),
])
def test_redact(msg, expected):
    assert ws.redact(msg) == expected


def test_sessions_summary_has_no_paths(server, home):
    srv = server()
    call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    assert wait(lambda: srv.service.engine.state == State.DETECTING)
    call(srv, "POST", "/recording/start")
    assert wait(lambda: srv.service.engine.state == State.RECORDING)
    call(srv, "POST", "/stop")
    status, body, _ = call(srv, "GET", "/sessions")
    assert status == 200 and body["sessions"][0]["recordings"] == 1
    assert body["sessions"][0]["source_id"] == "clip.mp4"
    assert str(home) not in json.dumps(body)


# --------------------------------------------------------------- shutdown
def test_close_stops_engine_and_finalizes_recording(make_service, repo, home):
    svc = make_service()
    srv = ws.MonitorHTTPServer(0, svc, SECRET)
    port = srv.server_port
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    assert wait(lambda: svc.engine.state == State.DETECTING)
    call(srv, "POST", "/recording/start")
    assert wait(lambda: svc.engine.state == State.RECORDING)
    srv.shutdown()
    srv.server_close()
    svc.close(timeout=20)
    assert not svc.engine.is_running and svc.engine.state == State.IDLE
    s = repo.get_session(svc.engine.last_report.session_id)
    assert s["status"] == "completed" and s["recordings"][0]["ended_at"] and s["recordings"][0]["ok"]
    with pytest.raises(ws.ServiceError) as info:
        svc.start("video:clip.mp4")
    assert info.value.status == 503
    # The port is released: a restarted service can listen on it again.
    again = ws.MonitorHTTPServer(port, svc, SECRET)
    again.server_close()


def test_second_server_cannot_share_port(server):
    srv = server()
    with pytest.raises(OSError):
        ws.MonitorHTTPServer(srv.server_port, srv.service, SECRET)


def test_cli_refuses_without_secret(home):
    env = {"ATCS_MONITOR_HOME": str(home), "PATH": "/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, str(ws.Path(ws.__file__)), "--port", "18765"], env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "ATCS_MONITOR_SERVICE_SECRET" in proc.stderr


def test_cli_serves_and_shuts_down_on_sigterm(home, videos):
    import os
    import signal

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "ATCS_MONITOR_HOME": str(home), "ATCS_MONITOR_SERVICE_SECRET": SECRET,
           "ATCS_MONITOR_VIDEO_DIR": str(videos), "ATCS_MONITOR_CAMERAS": "0"}
    proc = subprocess.Popen([sys.executable, str(ws.Path(ws.__file__)), "--port", str(port)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        def ready():
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                conn.request("GET", "/sources", headers={"Authorization": f"Bearer {SECRET}"})
                r = conn.getresponse()
                ok = r.status == 200 and json.loads(r.read())["sources"]
                conn.close()
                return ok
            except OSError:
                return False

        assert wait(ready, 60)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
        assert proc.returncode == 0
        assert SECRET not in err
        # Importing the service does not load YOLO / torch.
    finally:
        if proc.poll() is None:
            proc.kill()


def test_import_does_not_load_model_libraries():
    code = "import sys, web_service; print(any(m in sys.modules for m in ('torch', 'ultralytics')))"
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ws.Path(ws.__file__).parent),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


# ------------------------------------------------- crashed-session recovery
def desktop_session(db_path, session_id, status="running"):
    """A session written the way the desktop app writes it (plain Repository)."""
    r = Repository(db_path)
    r.create_session(session_id, "camera", "0:Desk cam", "yolo26n.pt", "v", "cpu", {})
    if status != "running":
        r.finish_session(session_id, status, {"unique": {}, "crossings": {}})
    rec = r.add_recording(session_id, f"/rec/{session_id}.mp4", "mp4v", "mp4", "2026-10-23T00:00:00+00:00")
    r.close()
    return rec


def session_row(db_path, session_id):
    r = Repository(db_path)
    try:
        s = r.get_session(session_id)
        return s["status"], s["error"], s["recordings"][0]["error"] if s["recordings"] else None
    finally:
        r.close()


def test_service_sessions_are_journaled(server, repo):
    srv = server()
    call(srv, "POST", "/start", body={"source": "video:clip.mp4", "loop": True})
    assert wait(lambda: srv.service.engine.state == State.DETECTING)
    sid = srv.service.engine.latest_snapshot().session_id or call(srv, "GET", "/status")[1]["session_id"]
    rows = repo._rows("SELECT session_id FROM web_service_sessions")
    assert [r["session_id"] for r in rows] == [sid]


def test_recovery_only_touches_sessions_owned_by_the_service(home):
    db_path = home / "shared.sqlite3"
    desktop_session(db_path, "desk-live")  # desktop app monitoring right now
    desktop_session(db_path, "desk-done", status="completed")
    svc_repo = ws.ServiceRepository(db_path)
    svc_repo.create_session("svc-crashed", "video_file", "clip.mp4", "yolo26n.pt", "v", "cpu", {})
    svc_repo.add_recording("svc-crashed", "/rec/svc.mp4", "mp4v", "mp4", "2026-10-23T00:00:00+00:00")
    svc_repo.create_session("svc-done", "video_file", "clip.mp4", "yolo26n.pt", "v", "cpu", {})
    svc_repo.finish_session("svc-done", "completed", {"unique": {}, "crossings": {}})

    assert svc_repo.recover_interrupted_sessions() == 1
    assert svc_repo.recover_interrupted_sessions() == 0
    svc_repo.close()
    status, error, rec_error = session_row(db_path, "svc-crashed")
    assert status == "interrupted" and error == "Monitor service ended unexpectedly"
    assert "monitor service ended unexpectedly" in rec_error
    assert session_row(db_path, "svc-done")[:2] == ("completed", None)
    assert session_row(db_path, "desk-live") == ("running", None, None)
    assert session_row(db_path, "desk-done")[:2] == ("completed", None)


def test_data_dir_lock_is_exclusive(home):
    a = ws.DataDirLock(home / "monitor_service.lock")
    b = ws.DataDirLock(home / "monitor_service.lock")
    a.acquire()
    with pytest.raises(ws.DataDirLocked):
        b.acquire()
    a.release()
    b.acquire()
    b.release()


CRASH_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[1])
import web_service as ws
from app.detection.types import Detection
from app.settings import Settings

class Det:
    model = object(); model_name = "yolo26n.pt"; device_pref = "auto"; tracker = "bytetrack"
    device = "cpu"; version = "scripted"
    def reset_tracker(self): pass
    def track(self, image, conf, iou, imgsz, classes=None):
        return [Detection("car", 0.9, 10, 10, 40, 40, 1)]

lock = ws.DataDirLock(ws.paths.data_dir() / "monitor_service.lock")
lock.acquire()
repo = ws.ServiceRepository(ws.paths.database_path())
svc = ws.MonitorService(Settings(max_processing_fps=0), repo, ws.SourceCatalog((), sys.argv[2]), detector=Det())
svc.start("video:clip.mp4", loop=True)
while not (svc.engine.latest_snapshot().frames_processed > 5):
    time.sleep(0.05)
svc.start_recording()
while svc.engine.state.value != "Recording":
    time.sleep(0.05)
print(svc.engine.latest_snapshot().session_id, flush=True)
time.sleep(600)
"""


def test_killed_service_session_is_recovered_without_touching_desktop(home, videos):
    import os
    import signal

    from app import paths

    db_path = paths.database_path()
    desktop_session(db_path, "desk-live")
    child = subprocess.Popen([sys.executable, "-c", CRASH_CHILD, str(ws.Path(ws.__file__).parent), str(videos)],
                             env={**os.environ, "ATCS_MONITOR_HOME": str(home)}, stdout=subprocess.PIPE, text=True)
    try:
        sid = child.stdout.readline().strip()
        assert len(sid) == 32
        lock = ws.DataDirLock(paths.data_dir() / "monitor_service.lock")
        with pytest.raises(ws.DataDirLocked):  # the live service owns the folder
            lock.acquire()
        child.send_signal(signal.SIGKILL)
        child.wait(30)
    finally:
        if child.poll() is None:
            child.kill()
    assert session_row(db_path, sid)[0] == "running"  # what the crash left behind
    lock.acquire()  # released by the OS when the process died
    try:
        repo = ws.ServiceRepository(db_path)
        assert repo.recover_interrupted_sessions() == 1
        repo.close()
    finally:
        lock.release()
    status, error, rec_error = session_row(db_path, sid)
    assert status == "interrupted" and error == "Monitor service ended unexpectedly"
    assert rec_error and "not finalized" in rec_error
    assert session_row(db_path, "desk-live") == ("running", None, None)


def test_second_cli_on_same_data_folder_refuses(home, videos):
    import os

    lock = ws.DataDirLock(ws.paths.data_dir() / "monitor_service.lock")
    lock.acquire()
    try:
        env = {**os.environ, "ATCS_MONITOR_HOME": str(home), "ATCS_MONITOR_SERVICE_SECRET": SECRET}
        proc = subprocess.run([sys.executable, str(ws.Path(ws.__file__)), "--port", "18767"], env=env,
                              capture_output=True, text=True, timeout=60)
    finally:
        lock.release()
    assert proc.returncode == 2 and "already using this data folder" in proc.stderr
