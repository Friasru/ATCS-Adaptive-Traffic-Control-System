"""Desktop link against the real ATCS backend (Flask in a subprocess, temporary database).

Needs the backend's Python environment: ATCS_BACKEND_PYTHON, or backend/venv or
the repository's .venv. Skipped when none can import Flask. Nothing touches
backend/atcs.db: each server uses its own database in pytest's tmp folder.
"""

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

from app.desktop_link import documents as d
from app.desktop_link.client import NetworkError, TelemetryClient
from app.desktop_link.outbox import Outbox
from app.desktop_link.service import LinkService
from app.storage.repository import Repository, iso
from tests.link_support import REPO_ROOT, link_config, local_session

HARNESS = Path(__file__).with_name("backend_harness.py")


def backend_python():
    candidates = [os.environ.get("ATCS_BACKEND_PYTHON"), REPO_ROOT / "backend" / "venv" / "bin" / "python",
                  REPO_ROOT / ".venv" / "bin" / "python", REPO_ROOT / "backend" / "venv" / "Scripts" / "python.exe",
                  REPO_ROOT / ".venv" / "Scripts" / "python.exe"]
    for c in candidates:
        if c and Path(c).is_file():
            ok = subprocess.run([str(c), "-c", "import flask, flask_sqlalchemy, flask_jwt_extended"],
                                capture_output=True).returncode == 0
            if ok:
                return str(c)
    pytest.skip("no backend Python environment with Flask found (set ATCS_BACKEND_PYTHON)")


class Backend:
    def __init__(self, tmp, **env):
        self.py = backend_python()
        self.db = tmp / "atcs-test.db"
        self.out = tmp / "backend.json"
        self.env = {**os.environ, **{k: str(v) for k, v in env.items()}}
        self.proc = None
        self.info = {}

    def start(self, port=0):
        if self.out.exists() and port == 0:
            self.out.unlink()
        before = self.out.stat().st_mtime_ns if self.out.exists() else None
        self.proc = subprocess.Popen([self.py, str(HARNESS), "serve", str(self.db), str(self.out), str(port)],
                                     env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.out.exists() and self.out.stat().st_mtime_ns != before:
                self.info = json.loads(self.out.read_text())
                break
            if self.proc.poll() is not None:
                raise RuntimeError("backend failed: " + self.proc.stderr.read().decode()[-2000:])
            time.sleep(0.05)
        else:
            raise RuntimeError("backend did not start")
        self.url = f"http://127.0.0.1:{self.info['port']}"
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)

    def revoke(self, name):
        subprocess.run([self.py, str(HARNESS), "revoke", str(self.db), name], env=self.env, check=True,
                       capture_output=True)

    def config(self, device="desk-a", token=None):
        return link_config(self.url, token or self.info["tokens"][device], device)

    def get(self, path):
        req = urllib.request.Request(self.url + path, headers={"Authorization": "Bearer " + self.info["jwt"]})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    b = Backend(tmp_path_factory.mktemp("backend")).start()
    yield b
    b.stop()


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "history.sqlite3")
    yield r
    r.close()


def make_service(backend, repo, tmp_path, device="desk-a", token=None, name="outbox.sqlite3"):
    box = Outbox(tmp_path / name)
    cfg = backend.config(device, token)
    return LinkService(cfg, repo, box, "1.0.0", client=TelemetryClient(cfg)), box


def drive(svc, until, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        delay = svc.run_once()
        if until():
            return True
        time.sleep(min(max(delay, 0.01), 0.2))
    return False


def test_live_status_then_completed_session(backend, repo, tmp_path):
    svc, box = make_service(backend, repo, tmp_path)
    events = [(i, "car" if i % 4 else "person", "a_to_b" if i % 2 else "b_to_a") for i in range(1, 1201)]
    sid = local_session(repo, status="running", events=[])
    started = repo.get_session(sid)["started_at"]
    svc.set_live(d.LiveState(state="Detecting", session_id=sid, timestamp=iso(), camera_status="ok",
                             visible={"car": 2}, unique={"car": 5}, crossings={"car": 0}, frames_processed=40,
                             inference_fps=12.5, device="cpu"))
    svc.run_once()
    status = backend.get("/api/monitor/desktop/status")
    dev = next(x for x in status["devices"] if x["desktop"]["name"] == "desk-a")
    assert dev["live"] is True and dev["running"] is True and dev["session_id"] == sid
    assert dev["unique"]["car"] == 5 and dev["source"] == {"kind": "camera", "label": "Integrated Camera"}
    assert dev["performance"]["inference_fps"] == 12.5

    # the session ends in the app: events and stats are saved locally
    repo.insert_events([(sid, iso(), "line-1", t, c, dr) for t, c, dr in events], [])
    tally = {c: sum(1 for _, k, _ in events if k == c) for c in ("person", "car")}
    repo.finish_session(sid, "completed", {"unique": {"car": 900, "person": 300}, "crossings": tally,
                                           "line_totals": {}, "duration_s": 60.0, "frames_processed": 1800,
                                           "avg_inference_fps": 30.0})
    svc.session_finished(sid)
    svc.set_live(d.LiveState(state="Idle", message="Stopped"))
    assert drive(svc, lambda: box.state_of(sid) == "done")
    stored = backend.get(f"/api/monitor/desktop/sessions/{sid}")
    assert stored["status"] == "completed" and stored["finalized"] is True
    assert stored["counts_origin"] == "summary" and stored["crossing_event_count"] == 1200
    assert stored["started_at"] == started and stored["device"] == "desk-a"
    assert stored["unique"]["car"] == 900 and stored["frames_processed"] == 1800
    assert stored["provenance"]["instance_id"] == svc.instance_id
    box.close()


def test_restart_reupload_does_not_duplicate(backend, repo, tmp_path):
    sid = local_session(repo, events=[(i, "car", "a_to_b") for i in range(1, 701)])
    svc, box = make_service(backend, repo, tmp_path)
    box.observe(sid)
    assert drive(svc, lambda: box.state_of(sid) == "done")
    box.close()
    # a new process with an outbox that lost its acknowledgement sends everything again
    svc2, box2 = make_service(backend, repo, tmp_path, name="outbox2.sqlite3")
    assert svc2.instance_id != svc.instance_id
    box2.observe(sid)
    assert drive(svc2, lambda: box2.state_of(sid) == "done")
    stored = backend.get(f"/api/monitor/desktop/sessions/{sid}")
    assert stored["crossing_event_count"] == 700
    sessions = backend.get("/api/monitor/desktop/sessions?limit=200")["sessions"]
    assert sum(1 for s in sessions if s["session_id"] == sid) == 1
    box2.close()


def test_crashed_session_keeps_last_live_counts(backend, repo, tmp_path):
    svc, box = make_service(backend, repo, tmp_path)
    sid = local_session(repo, status="running", events=[(1, "car", "a_to_b"), (2, "car", "a_to_b")])
    svc.set_live(d.LiveState(state="Detecting", session_id=sid, timestamp=iso(), unique={"car": 7},
                             crossings={"car": 2}, frames_processed=300, camera_status="ok"))
    svc.run_once()
    box.close()
    # crash; the next start marks the session interrupted and a new process uploads it
    repo.mark_interrupted_sessions()
    svc2, box2 = make_service(backend, repo, tmp_path)
    assert drive(svc2, lambda: box2.state_of(sid) == "done")
    stored = backend.get(f"/api/monitor/desktop/sessions/{sid}")
    assert stored["status"] == "interrupted" and stored["counts_origin"] == "last_live_update"
    assert stored["unique"]["car"] == 7 and stored["frames_processed"] == 300  # last live values, not zeros
    assert stored["crossings"]["car"] == 2
    box2.close()


def test_late_heartbeat_after_summary_changes_nothing(backend, repo, tmp_path):
    sid = local_session(repo, events=[(1, "car", "a_to_b")])
    svc, box = make_service(backend, repo, tmp_path)
    box.observe(sid)
    assert drive(svc, lambda: box.state_of(sid) == "done")
    info = d.session_info(repo.get_session(sid))
    late = d.build_status(d.LiveState(state="Detecting", session_id=sid, timestamp=iso(), unique={"car": 99}),
                          info, instance_id=svc.instance_id, instance_started_at=svc.instance_started_at, seq=10_000,
                          sent_at=iso(), app_version="1.0.0")
    resp = svc.client.post_status(late)
    assert resp.status == 200 and resp.body.get("session") == "finalized"
    assert backend.get(f"/api/monitor/desktop/sessions/{sid}")["unique"]["car"] == 1
    box.close()


def test_invalid_token_is_refused_and_nothing_stored(backend, repo, tmp_path):
    sid = local_session(repo, events=[(1, "car", "a_to_b")])
    svc, box = make_service(backend, repo, tmp_path, token="atcsdev_" + "x" * 43)
    box.observe(sid)
    svc.run_once()
    assert svc.status()["connection"] == "unauthorized"
    assert box.state_of(sid) == "pending" and box.rows()[0]["attempts"] == 0  # kept, not failed
    with pytest.raises(urllib.error.HTTPError):
        backend.get(f"/api/monitor/desktop/sessions/{sid}")
    box.close()


def test_probe_reports_token_state(backend):
    assert TelemetryClient(backend.config("desk-a")).probe().status == 415
    assert TelemetryClient(backend.config("desk-a", token="atcsdev_" + "y" * 43)).probe().status == 401


def test_device_isolation(backend, repo, tmp_path):
    sid = local_session(repo, events=[(1, "car", "a_to_b")])
    svc_a, box_a = make_service(backend, repo, tmp_path)
    box_a.observe(sid)
    assert drive(svc_a, lambda: box_a.state_of(sid) == "done")
    # desk-b cannot add to or overwrite desk-a's session
    svc_b, box_b = make_service(backend, repo, tmp_path, device="desk-b", name="outbox-b.sqlite3")
    box_b.observe(sid)
    assert drive(svc_b, lambda: box_b.state_of(sid) == "rejected")
    assert box_b.rows(("rejected",))[0]["last_code"] == "session_conflict"
    stored = backend.get(f"/api/monitor/desktop/sessions/{sid}")
    assert stored["device"] == "desk-a"
    box_a.close()
    box_b.close()


def test_revoked_device(tmp_path, repo):
    b = Backend(tmp_path).start()
    try:
        svc, box = make_service(b, repo, tmp_path)
        svc.run_once()
        assert svc.status()["connection"] == "connected"
        b.revoke("desk-a")
        sid = local_session(repo, events=[(1, "car", "a_to_b")])
        box.observe(sid)
        svc.poke()  # what session_finished does
        svc._next_heartbeat = 0
        svc.run_once()
        assert svc.status()["connection"] == "unauthorized"
        drive(svc, lambda: False, timeout=1.5)
        assert box.state_of(sid) == "pending"  # kept for when a new token is configured
        assert box.rows()[0]["attempts"] == 0
        box.close()
    finally:
        b.stop()


def test_backend_restart_during_upload(tmp_path, repo):
    b = Backend(tmp_path).start()
    try:
        svc, box = make_service(b, repo, tmp_path)
        sid = local_session(repo, events=[(i, "car", "a_to_b") for i in range(1, 1501)])
        box.observe(sid)
        assert drive(svc, lambda: box.rows() and box.rows()[0]["events_sent"] >= 500)
        port = b.info["port"]
        b.stop()
        drive(svc, lambda: svc.status()["connection"] == "offline", timeout=10)
        assert svc.status()["connection"] == "offline" and box.state_of(sid) == "pending"
        b.start(port)  # same database, same port
        assert drive(svc, lambda: box.state_of(sid) == "done", timeout=60)
        assert b.get(f"/api/monitor/desktop/sessions/{sid}")["crossing_event_count"] == 1500
        box.close()
    finally:
        b.stop()


def test_backend_rate_limit_is_respected(tmp_path, repo):
    b = Backend(tmp_path, ATCS_TEST_RATE=2, ATCS_TEST_BURST=3).start()
    try:
        svc, box = make_service(b, repo, tmp_path)
        sids = [local_session(repo, events=[(i, "car", "a_to_b") for i in range(1, 1001)]) for _ in range(2)]
        for sid in sids:
            box.observe(sid)
        seen_429 = []
        orig = svc._handle_config_or_rate

        def spy(resp, now, what):
            if resp.status == 429:
                seen_429.append(resp.retry_after)
            return orig(resp, now, what)

        svc._handle_config_or_rate = spy
        assert drive(svc, lambda: all(box.state_of(s) == "done" for s in sids), timeout=60)
        assert seen_429 and all(r and r >= 1 for r in seen_429)
        box.close()
    finally:
        b.stop()


def test_unreachable_backend_is_a_network_error(repo):
    client = TelemetryClient(link_config("http://127.0.0.1:9"), timeout=1.0)
    with pytest.raises(NetworkError):
        client.post_status({})
