"""Automatic start and stop of the 3D simulation service (app/services/simulator3d.py).

These start the real service process (simulation3d/atcs3d/managed.py, which needs aiohttp but no SUMO until a simulation
runs) on free ports, never on 8766. The SUMO check of the web app is switched off where it is not the subject; the
simulations themselves are tested in simulation3d/tests/test_autostart.py.
"""
import http.server
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

import pytest

from app import create_app
from app.services import simulator3d
from app.services.simulator3d import Simulator3DService, free_port, identify
from config import Config
from tests.conftest import ConfigForTesting

ROOT = Config.SIMULATOR3D_DIR
PERF = {"step_roundtrip_ms": {}, "frame_bytes": {}, "clients": 0}


def wait_for(predicate, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def settled(service):
    assert wait_for(lambda: service.status()["state"] != "starting"), service.status()
    return service.status()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return not os.path.exists(f"/proc/{pid}/stat") or open(f"/proc/{pid}/stat").read().split(") ")[1][0] != "Z"


@pytest.fixture
def popen_calls(monkeypatch):
    calls = []
    real = subprocess.Popen

    def spy(*args, **kwargs):
        if "atcs3d.managed" in args[0]:  # (the tests start other processes too)
            calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(simulator3d.subprocess, "Popen", spy)
    return calls


@pytest.fixture
def no_sumo_check(monkeypatch):
    monkeypatch.setattr(Simulator3DService, "_preflight", lambda self: None)


@pytest.fixture
def make_service(no_sumo_check):
    """A service on a free port; every one is stopped at the end of the test."""
    pytest.importorskip("aiohttp")
    made = []

    def make(**kwargs):
        kwargs.setdefault("port", free_port())
        service = Simulator3DService(ROOT, **kwargs)
        made.append(service)
        return service

    yield make
    for service in made:
        service.stop()


class FakeHttp:
    """Something else listening on a port: answers fixed JSON for the given paths, 404 for everything else."""

    def __init__(self, routes):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append(self.path)
                body = routes.get(self.path)
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *args):
                pass

        self.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def listeners():
    opened = []
    yield opened
    for item in opened:
        item.close()


def other_program(listeners):
    """A web server that is not a 3D service: it even answers /api/health with {"ok": true}."""
    fake = FakeHttp({"/api/health": b'{"ok": true}', "/": b"hello"})
    listeners.append(fake)
    return fake


def run_sh_service(listeners):
    """What simulation3d/run.sh serves: no /api/identity, but /api/health and /api/perf."""
    fake = FakeHttp({"/api/health": b'{"ok": true}', "/api/perf": b'{"step_roundtrip_ms": {}, "frame_bytes": {}, "clients": 0}'})
    listeners.append(fake)
    return fake


def fake_python(tmp_path, script):
    path = tmp_path / "python"
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# ------------------------------------------------------------------------------------------------ nothing at app start
@pytest.fixture
def exit_hooks(monkeypatch):
    """What the app asks the interpreter to run at exit (the tests call it themselves)."""
    registered = []
    monkeypatch.setattr(simulator3d.atexit, "register", registered.append)
    monkeypatch.setattr(simulator3d, "_stop_on_sigterm", lambda service: None)
    return registered


@pytest.fixture
def autostart_app(exit_hooks):
    class Autostart(ConfigForTesting):
        TESTING = False
        SIMULATOR3D_PORT = str(free_port())

    return create_app(Autostart)


def test_creating_the_app_and_asking_for_the_status_starts_nothing(autostart_app, exit_hooks, popen_calls):
    service = autostart_app.extensions["simulator3d"]
    assert service.enabled and exit_hooks == [service.stop]  # stopped when the app exits
    client = autostart_app.test_client()
    assert client.get("/").status_code == 200
    assert client.get("/simulator3d.html").status_code == 200
    assert client.get("/api/simulator3d/status").get_json() == {
        "state": "idle", "port": None, "owned": False, "message": "", "detail": ""}
    assert popen_calls == []


@pytest.mark.usefixtures("no_sumo_check")
def test_the_page_asking_starts_the_service_once_and_the_app_stops_it(autostart_app, exit_hooks, popen_calls):
    pytest.importorskip("aiohttp")
    service = autostart_app.extensions["simulator3d"]
    client = autostart_app.test_client()
    try:
        answers = []
        threads = [threading.Thread(target=lambda: answers.append(client.post("/api/simulator3d/start").get_json())) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert {a["state"] for a in answers} <= {"starting", "running"} and len(popen_calls) == 1
        assert settled(service)["state"] == "running"
        assert client.post("/api/simulator3d/start").get_json()["state"] == "running" and len(popen_calls) == 1
        port = service.status()["port"]
        pid = service._proc.pid
    finally:
        exit_hooks[0]()  # what the interpreter runs at exit
    assert not alive(pid) and not simulator3d.port_in_use(port)
    assert client.get("/api/simulator3d/status").get_json()["state"] == "idle"


def test_switched_off_or_testing_or_a_bad_port_never_starts_anything(monkeypatch, popen_calls):
    class Off(ConfigForTesting):
        TESTING = False
        SIMULATOR3D_AUTOSTART = False

    class BadPort(ConfigForTesting):
        TESTING = False
        SIMULATOR3D_PORT = "70000"

    class Elsewhere(ConfigForTesting):
        TESTING = False

    monkeypatch.setattr(simulator3d, "POSIX", False)  # (this one runs as if on Windows)
    elsewhere = create_app(Elsewhere)
    assert elsewhere.test_client().post("/api/simulator3d/start").get_json()["message"].startswith("The 3D service runs on Linux or WSL")
    monkeypatch.setattr(simulator3d, "POSIX", True)
    for config, text in [(ConfigForTesting, "while testing"), (Off, "ATCS3D_AUTOSTART=0"), (BadPort, "ATCS3D_PORT")]:
        app = create_app(config)
        client = app.test_client()
        for answer in (client.get("/api/simulator3d/status").get_json(), client.post("/api/simulator3d/start").get_json()):
            assert answer["state"] == "disabled" and text in answer["message"]
    assert popen_calls == []


def test_the_endpoints(client, app):
    assert client.get("/api/simulator3d/status").status_code == 200
    r = client.get("/api/simulator3d/start")
    assert r.status_code == 405 and r.get_json() == {"error": "method not allowed"}

    seen = []

    class Fake:
        def status(self):
            return {"state": "idle"}

        def ensure(self, retry=False):
            seen.append(retry)
            return {"state": "starting"}

    app.extensions["simulator3d"] = Fake()
    for body in (None, {}, {"retry": "yes"}, {"retry": True}, [True]):
        assert client.post("/api/simulator3d/start", json=body).get_json() == {"state": "starting"}
    assert seen == [False, False, False, True, False]  # only a real `true` asks for another try


def test_sigterm_stops_the_service_before_the_app_dies(monkeypatch):
    handlers, killed, stopped = {}, [], []
    monkeypatch.setattr(simulator3d.signal, "getsignal", lambda signum: signal.SIG_DFL)
    monkeypatch.setattr(simulator3d.signal, "signal", lambda signum, handler: handlers.__setitem__(signum, handler))
    monkeypatch.setattr(simulator3d.os, "kill", lambda pid, signum: killed.append(signum))

    class Service:
        def stop(self):
            stopped.append(True)

    simulator3d._stop_on_sigterm(Service())
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert stopped == [True] and killed == [signal.SIGTERM] and handlers[signal.SIGTERM] == signal.SIG_DFL


# ------------------------------------------------------------------------------------------------ the real service
def test_start_waits_for_the_real_service_and_stop_ends_only_it(make_service, popen_calls, listeners, tmp_path):
    other = other_program(listeners)
    bystander = subprocess.Popen(["sleep", "60"], start_new_session=True)
    service = make_service()
    try:
        first = service.ensure()
        assert first["state"] == "starting" and first["owned"] and first["port"] == service.preferred_port
        assert service.ensure()["state"] in ("starting", "running") and len(popen_calls) == 1
        assert settled(service)["state"] == "running"
        port, pid, instance = service.status()["port"], service._proc.pid, service._instance
        assert identify(port, instance) == "ours"  # it answered with the id this process gave it
        assert identify(port, "someone else") == "atcs3d"
        assert popen_calls[0][1:3] == ["-m", "atcs3d.managed"]
        with open(os.path.join(ROOT, "output", "service.log")) as f:
            assert "Traceback" not in f.read()

        service.stop()
        assert not alive(pid) and not simulator3d.port_in_use(port)
        assert service.status()["state"] == "idle" and service.status()["port"] is None
        service.stop()  # nothing left to stop
        assert bystander.poll() is None and other.requests == []  # neither the unrelated process nor port was touched
    finally:
        bystander.kill()
        bystander.wait()


def test_a_service_that_is_gone_is_started_again_when_asked(make_service):
    service = make_service()
    service.ensure()
    assert settled(service)["state"] == "running"
    old = service._proc.pid
    os.kill(old, signal.SIGKILL)
    assert wait_for(lambda: service.status()["state"] == "idle")
    service.ensure()
    assert settled(service)["state"] == "running" and service._proc.pid != old


def test_the_service_exits_when_the_app_holding_its_pipe_is_gone(no_sumo_check):
    """kill -9 of the web app closes the pipe: the service must not outlive it."""
    pytest.importorskip("aiohttp")
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-m", "atcs3d.managed", "--port", str(port)], cwd=ROOT, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            env=dict(os.environ, ATCS3D_INSTANCE="x", PYTHONDONTWRITEBYTECODE="1"))
    try:
        assert wait_for(lambda: identify(port, "x") == "ours")
        proc.stdin.close()
        assert proc.wait(timeout=15) == 0 or proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()


# ------------------------------------------------------------------------------------------------ ports and identity
def test_identify_tells_a_3d_service_from_anything_else(listeners):
    assert identify(free_port()) is None  # nobody there
    assert identify(other_program(listeners).port) is None  # answers /api/health, is not a 3D service
    perf_only = FakeHttp({"/api/perf": b"{}"})
    listeners.append(perf_only)
    assert identify(perf_only.port) is None
    assert identify(run_sh_service(listeners).port) == "atcs3d"
    mine = FakeHttp({"/api/identity": b'{"service": "atcs3d", "instance": "abc"}'})
    listeners.append(mine)
    assert identify(mine.port, "abc") == "ours" and identify(mine.port, "xyz") == "atcs3d" and identify(mine.port) == "atcs3d"
    sock = socket.socket()
    listeners.append(sock)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    assert identify(sock.getsockname()[1]) is None  # accepts connections, never answers


@pytest.mark.parametrize("kind", ["web", "socket"])
def test_a_port_used_by_another_program_is_left_alone_and_another_one_is_used(make_service, listeners, kind):
    other = None
    if kind == "web":
        other = other_program(listeners)
        port = other.port
    else:
        sock = socket.socket()
        listeners.append(sock)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
    service = make_service(port=port)
    answer = service.ensure()
    assert answer["owned"] and answer["port"] != port
    assert settled(service)["state"] == "running"
    assert identify(service.status()["port"], service._instance) == "ours"
    if other is not None:
        assert set(other.requests) == {"/api/identity", "/api/health", "/api/perf"}  # it was only asked who it is
        assert identify(port) is None
    service.stop()
    assert simulator3d.port_in_use(port)  # still theirs


def test_a_3d_service_started_by_hand_is_used_and_never_stopped(make_service, popen_calls, listeners):
    manual = run_sh_service(listeners)
    service = make_service(port=manual.port)
    answer = service.ensure()
    assert answer == {"state": "running", "port": manual.port, "owned": False, "message": "", "detail": ""}
    assert popen_calls == []
    service.stop()
    assert simulator3d.port_in_use(manual.port) and service.status()["state"] == "running"
    manual.close()
    assert service.ensure()["state"] in ("starting", "running") and len(popen_calls) == 1  # it is gone: ATCS starts its own


# ------------------------------------------------------------------------------------------------ failures
def test_missing_sumo_is_explained_and_not_retried_until_asked(monkeypatch, popen_calls):
    calls = []
    monkeypatch.setattr(simulator3d.shutil, "which", lambda name: calls.append(name))
    service = Simulator3DService(ROOT, free_port())
    answer = service.ensure()
    assert answer["state"] == "failed" and "SUMO is not installed" in answer["message"] and "apt install" in answer["message"]
    assert service.ensure()["state"] == "failed" and len(calls) == 1  # a polling page does not retry by itself
    monkeypatch.setattr(simulator3d.shutil, "which", lambda name: "/usr/bin/sumo")
    assert service.ensure()["state"] == "failed"
    pytest.importorskip("aiohttp")
    try:
        assert service.ensure(retry=True)["state"] == "starting"
        assert settled(service)["state"] == "running"
    finally:
        service.stop()


def test_a_service_that_dies_while_starting_shows_what_it_printed(tmp_path, no_sumo_check, popen_calls):
    python = fake_python(tmp_path, "echo \"ModuleNotFoundError: No module named 'aiohttp'\"; exit 1")
    service = Simulator3DService(ROOT, free_port(), python=python)
    service.ensure()
    status = settled(service)
    assert status["state"] == "failed" and "exit code 1" in status["message"] and not status["owned"]
    assert "No module named 'aiohttp'" in status["detail"]
    assert service.ensure()["state"] == "failed" and len(popen_calls) == 1


def test_a_service_that_never_answers_is_stopped_and_reported(tmp_path, no_sumo_check):
    python = fake_python(tmp_path, "echo $$ > " + str(tmp_path / "pid") + "; exec sleep 60")
    service = Simulator3DService(ROOT, free_port(), python=python, startup_timeout=1.0, stop_timeout=2.0)
    service.ensure()
    status = settled(service)
    assert status["state"] == "failed" and "did not answer within 1 seconds" in status["message"]
    assert not alive(int((tmp_path / "pid").read_text()))  # no child left behind


def test_a_program_that_cannot_be_run_is_reported(tmp_path, no_sumo_check):
    service = Simulator3DService(ROOT, free_port(), python=str(tmp_path / "missing"))
    answer = service.ensure()
    assert answer["state"] == "failed" and "could not be started" in answer["message"]
