"""Desktop link attached to the real main window (pytest-qt, offscreen).

Uses the scripted detector from test_engine.py and a generated video, so no
model weights, camera or backend are needed. The backend is a FakeClient that
validates every document with the backend's schema.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QLabel

from app.desktop_link import config, instance, launcher, service
from app.services.monitoring import State
from app.storage.repository import Repository
from app.ui.main_window import MainWindow
from tests.link_support import TOKEN, FakeClient
from tests.test_engine import ScriptedDetector, make_video, settings

pytestmark = pytest.mark.gui
MONITOR_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def make_window(qtbot, home):
    made = []

    def _make():
        (home / "data").mkdir(exist_ok=True)
        (home / "models").mkdir(exist_ok=True)
        (home / "models" / "yolo26n.pt").write_bytes(b"scripted detector: weights are never loaded")
        repo = Repository(home / "data" / "gui.sqlite3")
        w = MainWindow(settings(model_name="yolo26n.pt"), repo)
        w.engine.detector = ScriptedDetector()
        qtbot.addWidget(w)
        w.show()
        qtbot.waitUntil(lambda: not w._discovering, timeout=15000)
        made.append((w, repo))
        return w

    yield _make
    for w, repo in made:
        if w.engine.is_running:
            w.engine.stop(wait=True)
        w.engine.join()
        repo.close()


@pytest.fixture
def linked(home, monkeypatch):
    """Configure the link and replace the HTTP client with a FakeClient."""
    config.save_token(config.Secret(TOKEN))
    config.save_config("http://127.0.0.1:5000", "gui-test")
    fake = FakeClient()
    monkeypatch.setattr(service, "TelemetryClient", lambda cfg: fake)
    monkeypatch.setattr(instance, "activation_name", lambda: f"atcs-link-test-{id(fake)}")
    return fake


def link_label(w):
    return w.statusBar().findChild(QLabel, "desktop_link_status")


def test_standalone_without_configuration(make_window):
    w = make_window()
    assert launcher.attach(w) is None
    assert link_label(w) is None
    assert not [lab for lab in w.statusBar().findChildren(QLabel) if lab.text().startswith("ATCS")]


def test_broken_configuration_runs_standalone(make_window, home):
    config.save_config("http://127.0.0.1:5000", "gui-test")  # no token stored
    w = make_window()
    assert launcher.attach(w) is None
    labels = [lab.text() for lab in w.statusBar().findChildren(QLabel)]
    assert "ATCS: link not set up" in labels
    assert w.start_btn.isEnabled() or w.source_combo.currentData() is None  # the app itself is unaffected


def test_linked_session_is_sent(make_window, linked, qtbot, home):
    w = make_window()
    cleanup = launcher.attach(w)
    assert cleanup is not None
    label = link_label(w)
    assert label is not None
    qtbot.waitUntil(lambda: label.text().startswith("ATCS: connected"), timeout=5000)
    assert "video never leaves this computer" in w.statusBar().currentMessage()
    # opening the linked app never starts monitoring
    qtbot.wait(300)
    assert w.state == State.IDLE and not w.engine.is_running
    assert all(doc["session"] is None and doc["state"] == "Idle" for k, _, doc in linked.calls if k == "status")

    w.add_video_source(str(make_video(home / "clip.mp4", n=30)))
    w.start_monitoring()
    qtbot.waitUntil(lambda: any(k == "status" and doc["session"] for k, _, doc in linked.calls), timeout=15000)
    qtbot.waitUntil(lambda: w.state == State.IDLE and not w.engine.is_running, timeout=30000)
    sid = w.engine.last_report.session_id
    qtbot.waitUntil(lambda: sid in linked.summaries, timeout=15000)

    live = [doc for k, _, doc in linked.calls if k == "status" and doc["session"]]
    assert {doc["session"]["session_id"] for doc in live} == {sid}
    assert live[0]["source"] == {"kind": "video_file", "label": "clip.mp4"}
    assert all(doc["state"] in ("Detecting", "Stopping", "Initializing") for doc in live)
    summary = linked.summaries[sid]
    local = w.repo.get_session(sid)
    assert summary["crossings"] == {c: local["stats"]["crossings"].get(c, 0) for c in summary["crossings"]}
    assert summary["crossings"]["car"] == 1 and summary["stats_complete"] is True
    assert summary["started_at"] == local["started_at"] and summary["ended_at"] == local["ended_at"]
    kinds = [k for k, s, _ in linked.calls if s == sid]
    assert kinds == ["events", "summary"]
    # cumulative values in the heartbeats never decrease, and never exceed the summary
    for key in ("unique", "crossings"):
        for c in summary[key]:
            vals = [doc["session"][key][c] for doc in live]
            assert vals == sorted(vals) and (not vals or vals[-1] <= summary[key][c])
    # after the session the heartbeat no longer carries it
    qtbot.waitUntil(lambda: [d for k, _, d in linked.calls if k == "status"][-1]["session"] is None, timeout=15000)
    cleanup()
    assert not any(t.name == "desktop-link" and t.is_alive() for t in __import__("threading").enumerate())


def test_recording_state_is_reported(make_window, linked, qtbot, home):
    w = make_window()
    cleanup = launcher.attach(w)
    w.add_video_source(str(make_video(home / "long.mp4", n=60)))
    w.start_monitoring()
    qtbot.waitUntil(lambda: w.state == State.DETECTING, timeout=15000)
    w.start_recording()
    # the file name follows with the first recorded frame (it may be null for one heartbeat)
    qtbot.waitUntil(lambda: any(k == "status" and doc["recording"]["active"] and doc["recording"]["file"]
                                for k, _, doc in linked.calls), timeout=15000)
    rec = next(doc for k, _, doc in linked.calls if k == "status" and doc["recording"]["file"])
    assert rec["state"] == "Recording" and rec["recording"]["file"].endswith((".mp4", ".avi", ".mkv"))
    assert "/" not in rec["recording"]["file"]
    w.stop_monitoring()
    qtbot.waitUntil(lambda: not w.engine.is_running, timeout=30000)
    sid = w.engine.last_report.session_id
    qtbot.waitUntil(lambda: sid in linked.summaries, timeout=20000)
    assert len(linked.summaries[sid]["recordings"]) == 1
    cleanup()


def test_gui_stays_responsive_while_backend_hangs(make_window, linked, qtbot, monkeypatch, home):
    def hanging(doc):
        time.sleep(3)  # like a request waiting for its timeout
        return FakeClient.post_status(linked, doc)

    monkeypatch.setattr(linked, "post_status", hanging)
    w = make_window()
    cleanup = launcher.attach(w)
    qtbot.wait(200)  # the sender is now blocked inside a request
    gaps = []
    timer = QElapsedTimer()
    timer.start()

    def tick():
        gaps.append(timer.restart())

    t = QTimer()
    t.timeout.connect(tick)
    t.start(20)
    w.add_video_source(str(make_video(home / "clip.mp4", n=30)))
    w.start_monitoring()
    qtbot.wait(1500)
    t.stop()
    assert len(gaps) > 30 and max(gaps[1:]) < 500  # the event loop never froze
    assert w.engine.is_running or w.state == State.IDLE
    w.stop_monitoring()
    qtbot.waitUntil(lambda: not w.engine.is_running, timeout=30000)
    cleanup()


SECOND_LAUNCH = """
import sys
from PySide6.QtNetwork import QLocalSocket
from app.desktop_link import instance
instance.activation_name = lambda: sys.argv[1]
if len(sys.argv) == 2:
    print(instance.activate_running())
else:
    sock = QLocalSocket()
    sock.connectToServer(sys.argv[1])
    assert sock.waitForConnected(1000)
    sock.write(bytes.fromhex(sys.argv[2]))
    print(sock.waitForBytesWritten(1000) or not sock.bytesToWrite())
    sock.disconnectFromServer()
"""


def second_launch(qtbot, payload=None):
    """Talk to the running app from another process, as a second launch does, while the GUI thread keeps
    serving events. (On Windows a pipe write completes only when the server reads it, and a client thread
    in this process would hold the GIL while it waits.) Returns the client's printed result."""
    args = [sys.executable, "-c", SECOND_LAUNCH, instance.activation_name()] + ([payload.hex()] if payload else [])
    proc = subprocess.Popen(args, cwd=MONITOR_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    qtbot.waitUntil(lambda: proc.poll() is not None, timeout=15000)
    out, err = proc.communicate()
    assert proc.returncode == 0, err
    return out.strip() == "True"


def test_second_launch_activates_window(make_window, linked, qtbot, monkeypatch):
    from app.desktop_link.qt_link import DesktopLink

    w = make_window()
    raised = []
    monkeypatch.setattr(DesktopLink, "bring_to_front", lambda self: raised.append(True))
    cleanup = launcher.attach(w)
    assert second_launch(qtbot) is True
    qtbot.waitUntil(lambda: raised == [True], timeout=5000)
    cleanup()
    assert second_launch(qtbot) is False  # nothing listens once the app has closed


@pytest.mark.skipif(sys.platform != "win32", reason="a Unix socket buffers the request without the server reading it")
def test_busy_window_reports_activation_failure(make_window, linked, qtbot):
    """If the running app does not read the request in time, the launcher must not believe it was delivered."""
    w = make_window()
    cleanup = launcher.attach(w)
    assert instance.activate_running(timeout_ms=300) is False  # the GUI thread is blocked in this call
    cleanup()


def test_only_the_activate_message_is_accepted(make_window, linked, qtbot, monkeypatch):
    from app.desktop_link.qt_link import DesktopLink

    w = make_window()
    raised = []
    received = []
    monkeypatch.setattr(DesktopLink, "bring_to_front", lambda self: raised.append(True))
    real_on_request = DesktopLink._on_request
    monkeypatch.setattr(DesktopLink, "_on_request",
                        lambda self, sock: (received.append(sock.bytesAvailable()), real_on_request(self, sock)))
    cleanup = launcher.attach(w)
    payloads = (b"start\n", b"activate --start\n", b"\x00" * 64)
    for payload in payloads:
        assert second_launch(qtbot, payload) is True
    qtbot.waitUntil(lambda: len(received) >= len(payloads), timeout=5000)  # the requests did arrive
    qtbot.wait(300)
    assert raised == [] and not w.engine.is_running
    cleanup()
