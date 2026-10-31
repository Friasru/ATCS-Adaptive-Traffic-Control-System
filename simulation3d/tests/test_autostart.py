"""The 3D service as the ATCS web app starts and stops it, with real SUMO and a real browser.

  * atcs3d.managed (the service as the web app runs it): the same server plus /api/identity, and it ends, simulations and
    SUMO included, on SIGTERM and when the web app's end of its stdin pipe closes (what a crash of the web app does);
  * the web app's manager (backend/app/services/simulator3d.py) running every controller and side by side;
  * the 3D Simulator page asking for the service by itself: no second terminal, and a start that fails is explained.

The manager's own rules (ports, identity, failures) are tested without SUMO in backend/tests/test_simulator3d_service.py.
"""
import asyncio
import os
import signal
import subprocess
import sys
import threading
import time

import aiohttp
import pytest

from atcs3d import paths
from tests.browser_util import Browser
from tests.helpers import WsClient, free_port, sumo_processes
from tests.test_dashboard_integration import BACKEND, EXPECTED_WHEN_DOWN, frame_of, run, settle_idle, unexpected

pytestmark = pytest.mark.filterwarnings("ignore")

sys.path.insert(0, BACKEND)
try:
    from app import create_app
    from app.services import simulator3d
    from config import Config
finally:
    sys.path.remove(BACKEND)


def get(port, path):
    return simulator3d._get_json(port, path)


class Managed:
    """`python -m atcs3d.managed` the way the web app starts it: stdin is a pipe that this test holds open."""

    def __init__(self):
        self.port = free_port()

    def __enter__(self):
        self.proc = subprocess.Popen([sys.executable, "-m", "atcs3d.managed", "--port", str(self.port)], cwd=paths.ROOT,
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, env=dict(os.environ, ATCS3D_INSTANCE="test-instance", PYTHONDONTWRITEBYTECODE="1"))
        end = time.time() + 15
        while get(self.port, "/api/identity") is None:
            assert time.time() < end and self.proc.poll() is None, "service did not start"
            time.sleep(0.1)
        return self

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()


async def running_run(port, controllers=("fixed", "queue_based")):
    """A browser-like client that has a simulation running; returns once SUMO is stepping."""
    c = await WsClient(f"http://127.0.0.1:{port}").__aenter__()
    await c.idle()
    await c.send("speed", value=20)
    await c.send("select", controllers=list(controllers), scenario="uneven")
    await c.send("start")
    await c.until(lambda m: m["type"] == "frame" and m["t"] >= 3, 60)
    assert len(sumo_processes()) == 2 * len(controllers)
    return c


def wait_gone(timeout=10):
    end = time.time() + timeout
    while sumo_processes() and time.time() < end:
        time.sleep(0.1)
    return sumo_processes()


# ------------------------------------------------------------------------------------------------ the managed service
def test_it_serves_what_the_plain_service_serves_and_says_who_it_is():
    with Managed() as svc:
        assert get(svc.port, "/api/identity")["service"] == "atcs3d"
        assert get(svc.port, "/api/identity")["instance"] == "test-instance" and get(svc.port, "/api/identity")["pid"] == svc.proc.pid
        assert get(svc.port, "/api/health") == {"ok": True}
        assert {"step_roundtrip_ms", "frame_bytes", "clients"} <= get(svc.port, "/api/perf").keys()
        assert simulator3d.identify(svc.port, "test-instance") == "ours"

        async def go():
            async with aiohttp.ClientSession() as s:
                async with s.get(f"http://127.0.0.1:{svc.port}/") as r:
                    assert r.status == 200 and "text/html" in r.headers["Content-Type"]
                with pytest.raises(aiohttp.WSServerHandshakeError) as err:  # another web page may not drive the simulator
                    await s.ws_connect(f"http://127.0.0.1:{svc.port}/ws", headers={"Origin": "http://evil.example"})
                assert err.value.status == 403
        run(go())


def test_sigterm_with_a_browser_attached_ends_the_service_and_every_sumo_quickly():
    async def go(svc):
        c = await running_run(svc.port)
        t = time.time()
        svc.proc.send_signal(signal.SIGTERM)
        await asyncio.get_running_loop().run_in_executor(None, svc.proc.wait, 10)
        took = time.time() - t
        await c.__aexit__(None, None, None)
        return took

    with Managed() as svc:
        took = run(go(svc))
    assert svc.proc.returncode == 0 and took < 5, took
    assert wait_gone() == []


def test_when_the_web_app_dies_its_pipe_closes_and_the_service_and_every_sumo_go_with_it():
    async def go(svc):
        c = await running_run(svc.port)
        svc.proc.stdin.close()  # what the kernel does when the web app is killed
        await asyncio.get_running_loop().run_in_executor(None, svc.proc.wait, 15)
        await c.__aexit__(None, None, None)

    with Managed() as svc:
        run(go(svc))
    assert wait_gone() == []


# ------------------------------------------------------------------------------------------------ started by the manager
def test_the_managers_service_runs_every_controller_and_side_by_side_and_stop_leaves_nothing():
    service = simulator3d.Simulator3DService(paths.ROOT, free_port())

    async def go():
        service.ensure()
        assert await asyncio.get_running_loop().run_in_executor(None, wait_running, service)
        async with WsClient(f"http://127.0.0.1:{service.status()['port']}") as c:
            await c.idle()
            await c.send("speed", value=50)
            for controllers in (["fixed"], ["queue_based"], ["wait_time"], ["fixed", "queue_based"]):
                await c.send("select", controllers=controllers, scenario="uneven")
                await c.send("start")
                frame = await c.until(lambda m: m["type"] == "frame" and m["t"] >= 20, 90)
                assert [p["controller"] for p in frame["panes"]] == controllers
                assert all(len(p["vehicles"]) > 0 for p in frame["panes"])
                assert len({p["t"] for p in frame["panes"]}) == 1  # side by side runs in lockstep
                await c.send("stop")
                await c.until_state("stopped", 30)
            await c.send("start")
            await c.until(lambda m: m["type"] == "frame", 30)
            assert sumo_processes()
        pid, port = service._proc.pid, service.status()["port"]
        await asyncio.get_running_loop().run_in_executor(None, service.stop)
        return pid, port

    pid, port = run(go())
    assert not simulator3d.port_in_use(port) and wait_gone() == []
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def wait_running(service, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if service.status()["state"] == "running":
            return True
        time.sleep(0.1)
    return False


# ------------------------------------------------------------------------------------------------ the page
@pytest.fixture(scope="module")
def autostart_site():
    """The real Flask app as `python run.py` builds it, with automatic start on, serving the real pages."""
    from werkzeug.serving import make_server

    class AutostartConfig(Config):
        TESTING = False
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        JWT_SECRET_KEY = "test-only-secret-key-at-least-32-bytes-long"
        SIMULATOR3D_PORT = str(free_port())

    mp = pytest.MonkeyPatch()
    mp.setattr(simulator3d, "_stop_on_sigterm", lambda service: None)  # pytest keeps its own signal handling
    app = create_app(AutostartConfig)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    service = app.extensions["simulator3d"]
    yield f"http://localhost:{server.server_port}", service
    server.shutdown()
    service.stop()
    mp.undo()


def test_opening_the_3d_tab_starts_the_service_and_nothing_else_does(autostart_site):
    site, service = autostart_site

    async def go():
        async with Browser() as b:
            pg = b.page
            for name in ("index.html", "scenarios.html", "compare.html", "simulator.html"):
                await pg.goto(f"{site}/{name}")
            assert service.status()["state"] == "idle"  # the other pages never start it

            await pg.goto(f"{site}/index.html")
            await pg.click("nav a[href='simulator3d.html']")  # no ?port, no second terminal
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service running'", timeout=30000)
            frame = await frame_of(pg)
            assert frame.url == f"http://localhost:{service.status()['port']}/"
            assert service.status()["owned"] and sumo_processes() == []  # no SUMO before Start

            await settle_idle(frame)
            await frame.select_option("#sel-scenario", "uneven")
            await frame.click("#speeds button[data-speed='20']")
            await frame.select_option("#sel-ctrl", "fixed")
            await frame.click("#btn-start")
            await frame.wait_for_function("window.__atcs.status === 'running' && window.__atcs.lastFrame "
                                          "&& window.__atcs.lastFrame.panes[0].controller === 'fixed' && window.__atcs.lastFrame.t >= 10", timeout=60000)
            assert len(sumo_processes()) == 2
            for ctrl in ("queue_based", "wait_time"):  # choosing another controller starts a fresh run of it
                rid = await frame.evaluate("window.__atcs.runId")
                await frame.select_option("#sel-ctrl", ctrl)
                await frame.wait_for_function(f"window.__atcs.runId > {rid} && window.__atcs.status === 'running'", timeout=30000)
                await frame.wait_for_function(f"window.__atcs.lastFrame.panes[0].controller === '{ctrl}' && window.__atcs.lastFrame.t >= 5", timeout=60000)
                assert len(sumo_processes()) == 2
            await frame.click("#btn-stop")
            await frame.wait_for_function("window.__atcs.status === 'stopped'")
            await frame.click("#btn-mode")
            await frame.select_option("#sel-ctrl", "fixed")
            await frame.select_option("#sel-ctrl2", "queue_based")
            await frame.click("#btn-start")
            await frame.wait_for_function("window.__atcs.status === 'running' && window.__atcs.lastFrame "
                                          "&& window.__atcs.lastFrame.panes.length === 2 && window.__atcs.lastFrame.t >= 10", timeout=60000)
            assert len(sumo_processes()) == 4

            # the app exits while the page is open and a simulation is running: everything the app started goes
            port = service.status()["port"]
            await asyncio.get_running_loop().run_in_executor(None, service.stop)
            assert not simulator3d.port_in_use(port) and wait_gone() == []
            await pg.wait_for_function("document.querySelector('#service-pill').textContent !== 'Service running'", timeout=15000)

            # ... and asking again (the page does, by itself) brings a fresh one up
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service running'", timeout=30000)
            assert service.status()["state"] == "running"
            assert unexpected(b.errors, EXPECTED_WHEN_DOWN + ("WebSocket", "ERR_")) == []
    run(go())
    service.stop()
    assert wait_gone() == []


def test_a_start_that_fails_is_explained_on_the_page_and_try_again_works(autostart_site, monkeypatch):
    site, service = autostart_site
    service.stop()
    service._state = "idle"
    monkeypatch.setattr(simulator3d.Simulator3DService, "_preflight",
                        lambda self: "SUMO is not installed (the 'sumo' command was not found).")

    async def go():
        async with Browser() as b:
            pg = b.page
            await pg.goto(f"{site}/simulator3d.html")
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service not running'", timeout=15000)
            assert await pg.is_visible("#unavailable") and not await pg.is_visible("#stage")
            assert "SUMO is not installed" in await pg.inner_text("#unavailable-reason")
            assert await pg.is_visible("#retry")
            assert "simulation3d/run.sh" in await pg.inner_text("#unavailable")  # the by-hand way is still offered
            await asyncio.sleep(3)
            assert service.status()["state"] == "failed"  # the page's polling did not start it in a loop

            monkeypatch.undo()  # SUMO is installed again
            await pg.click("#retry")
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service running'", timeout=30000)
            assert not await pg.is_visible("#unavailable") and await pg.is_visible("#stage")
            assert not await pg.is_visible("#retry")
            assert unexpected(b.errors, EXPECTED_WHEN_DOWN + ("ERR_",)) == []
    run(go())
    service.stop()


def test_a_page_given_a_port_only_looks_for_a_service_started_by_hand(autostart_site):
    site, service = autostart_site
    service.stop()

    async def go():
        async with Browser() as b:
            pg = b.page
            await pg.goto(f"{site}/simulator3d.html?port={free_port()}")
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service not running'", timeout=10000)
            await asyncio.sleep(2)
            assert "simulation3d/run.sh" in await pg.inner_text("#unavailable") and not await pg.is_visible("#retry")
            assert unexpected(b.errors, EXPECTED_WHEN_DOWN) == []
    run(go())
    assert service.status()["state"] == "idle"  # it did not ask the app to start anything
