"""The 3D Simulator tab of the ATCS website, end to end: the real Flask site, the real 3D service, real SUMO.

Chromium (software WebGL) opens the page the Flask app serves, which shows the service in a frame; everything below the
frame is the same code as the standalone 3D page, tested in test_web.py. These tests cover what the dashboard adds:
the tab, the page, the connection status, and that a missing or restarted service never breaks anything else.
"""
import asyncio
import os
import sys
import threading
import time

import pytest

from atcs3d import paths
from tests.browser_util import Browser
from tests.helpers import ServerProc, free_port, sumo_processes

pytestmark = pytest.mark.filterwarnings("ignore")

BACKEND = os.path.join(paths.REPO_ROOT, "backend")
NAV = [
    ("Home", "index.html"), ("Scenarios", "scenarios.html"), ("Dashboard", "dashboard.html"), ("Monitor", "monitor.html"),
    ("Simulator", "simulator.html"), ("Compare", "compare.html"), ("3D Simulator", "simulator3d.html"),
]
# A refused connection is what the page is *looking for* while the service is down; Chromium logs it as a console error.
EXPECTED_WHEN_DOWN = ("ERR_CONNECTION_REFUSED", "Failed to load resource")


@pytest.fixture(scope="module")
def site():
    """The real Flask app (the backend's create_app, throw-away in-memory database) serving the real frontend/ pages."""
    pytest.importorskip("flask_jwt_extended")
    pytest.importorskip("flask_bcrypt")
    pytest.importorskip("flask_sqlalchemy")
    from werkzeug.serving import make_server

    sys.path.insert(0, BACKEND)
    try:
        from app import create_app
        from config import Config
    finally:
        sys.path.remove(BACKEND)

    class TestConfig(Config):
        TESTING = True
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        JWT_SECRET_KEY = "test-only-secret-key-at-least-32-bytes-long"

    server = make_server("127.0.0.1", 0, create_app(TestConfig), threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://localhost:{server.server_port}"  # "localhost", as on Demo Day
    server.shutdown()


def run(coro):
    return asyncio.run(coro)


def unexpected(errors, allowed=()):
    return [e for e in errors if not any(a in e for a in allowed)]


def service_on(port):
    """A 3D service on a chosen port (ServerProc picks a free one by default)."""
    sp = ServerProc()
    sp.port, sp.url = port, f"http://127.0.0.1:{port}"
    return sp


async def frame_of(page):
    """The 3D page inside the frame, ready to use."""
    await page.wait_for_selector("#stage:not([hidden])", timeout=20000)
    frame = await (await page.query_selector("#stage")).content_frame()
    await frame.wait_for_function("window.__atcs && document.querySelector('#sel-ctrl').options.length > 0", timeout=20000)
    return frame


async def settle_idle(frame):
    if await frame.is_enabled("#btn-stop"):
        await frame.click("#btn-stop")
    await frame.wait_for_function("!document.querySelector('#btn-start').disabled", timeout=20000)


# ------------------------------------------------------------------------------------------------ navigation
def test_the_tab_follows_compare_on_every_page_and_every_link_works(site):
    async def go():
        async with Browser() as b:
            pg = b.page
            for name, current in [(f, f) for _, f in NAV]:
                await pg.goto(f"{site}/{name}")
                links = await pg.eval_on_selector_all(
                    "nav a", "as => as.map(a => [a.textContent.trim(), a.getAttribute('href'), a.getAttribute('aria-current')])")
                assert [l[:2] for l in links] == [list(n) for n in NAV], name
                assert [l[1] for l in links if l[2] == "page"] == [current], name
            # follow the real links, in order, from the home page
            await pg.goto(f"{site}/index.html")
            for _, href in NAV[1:]:
                await pg.click(f"nav a[href='{href}']")
                await pg.wait_for_url(f"**/{href}")
                assert await pg.is_visible("header .brand-name"), href
            assert await pg.inner_text("h1") == "3D Simulator"
            assert await pg.title() == "ATCS - 3D Simulator"
    run(go())


def test_compare_and_simulator_pages_still_run_next_to_the_new_tab(site):
    """Ian's 2D pages are untouched: the Compare page still animates its two intersections."""
    async def go():
        async with Browser() as b:
            pg = b.page
            await pg.goto(f"{site}/compare.html")
            await pg.click("#toggle")
            await pg.wait_for_function("document.querySelector('#time').textContent.split(' / ')[0] !== '0:00'", timeout=15000)
            assert await pg.inner_text("#left-light") != "-"
            await pg.goto(f"{site}/simulator.html")
            assert await pg.is_visible("main")
            assert unexpected(b.errors) == []
    run(go())


# ------------------------------------------------------------------------------------------------ service missing
def test_without_the_service_the_page_explains_how_to_start_it_and_nothing_else_breaks(site):
    async def go():
        async with Browser() as b:
            pg = b.page
            await pg.goto(f"{site}/simulator3d.html?port={free_port()}")
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service not running'", timeout=10000)
            assert await pg.is_visible("#unavailable")
            assert "simulation3d/run.sh" in await pg.inner_text("#unavailable")
            assert not await pg.is_visible("#stage")
            assert await pg.is_visible("nav a[href='compare.html']")
            await pg.click("nav a[href='compare.html']")
            await pg.wait_for_url("**/compare.html")
            assert unexpected(b.errors, EXPECTED_WHEN_DOWN) == []
    run(go())


# ------------------------------------------------------------------------------------------------ the real thing
def test_page_finds_the_service_when_it_starts_and_drives_real_sumo(site):
    port = free_port()

    async def go():
        async with Browser() as b:
            pg = b.page
            # 1. nothing is running yet
            await pg.goto(f"{site}/simulator3d.html?port={port}&shadows=0&dpr=1")
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service not running'", timeout=10000)

            # 2. the service starts: the page notices and loads it, passing the query on
            with service_on(port):
                await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service running'", timeout=20000)
                frame = await frame_of(pg)
                assert frame.url == f"http://localhost:{port}/?shadows=0&dpr=1"
                assert await pg.get_attribute("#open-tab", "href") == f"http://localhost:{port}/?shadows=0&dpr=1"
                await frame.wait_for_function("window.__atcs.renderer().calls > 20", timeout=20000)
                assert await frame.inner_text("#status-pill") == "Ready"
                assert len(await pg.screenshot()) > 40_000, "page looks empty"
                assert len(sumo_processes()) == 0, "no SUMO may run before Start is pressed"

                # 3. Fixed, start: real SUMO frames arrive, the clock is SUMO's
                await settle_idle(frame)
                await frame.select_option("#sel-scenario", "uneven")
                await frame.select_option("#sel-ctrl", "fixed")
                await frame.click("#speeds button[data-speed='20']")
                await frame.click("#btn-start")
                await frame.wait_for_function("window.__atcs.status === 'running' && window.__atcs.lastFrame && window.__atcs.lastFrame.t >= 40",
                                              timeout=60000)
                last = await frame.evaluate("window.__atcs.lastFrame")
                pane = last["panes"][0]
                assert pane["controller"] == "fixed" and len(pane["vehicles"]) > 0
                assert set(pane["tls"]) == {"state", "phase", "elapsed"} and len(pane["tls"]["state"]) == 16
                assert len(sumo_processes()) == 2  # one worker and its SUMO

                # 4. pause freezes SUMO's clock; resume continues
                await frame.click("#btn-pause")
                await frame.wait_for_function("window.__atcs.status === 'paused'")
                await pg.wait_for_timeout(300)
                t1 = await frame.evaluate("window.__atcs.lastFrame.t")
                await pg.wait_for_timeout(1500)
                assert await frame.evaluate("window.__atcs.lastFrame.t") == t1
                await frame.click("#btn-pause")
                await frame.wait_for_function(f"window.__atcs.lastFrame.t > {t1}")

                # 5. restart: a fresh run, with no second SUMO left behind
                rid = await frame.evaluate("window.__atcs.runId")
                await frame.click("#btn-restart")
                await frame.wait_for_function(f"window.__atcs.runId > {rid} && window.__atcs.status === 'running'", timeout=30000)
                await frame.wait_for_function("window.__atcs.lastFrame.t < 6")
                assert len(sumo_processes()) == 2, sumo_processes()

                # 6. the other controllers, then side by side
                for ctrl, title in [("queue_based", "ATCS Adaptive Control · Queue-Based"), ("wait_time", "ATCS Adaptive Control · Wait-Time")]:
                    rid = await frame.evaluate("window.__atcs.runId")
                    await frame.select_option("#sel-ctrl", ctrl)
                    await frame.wait_for_function(f"window.__atcs.runId > {rid} && window.__atcs.status === 'running'", timeout=30000)
                    await frame.wait_for_function(f"window.__atcs.lastFrame.panes[0].controller === '{ctrl}' && window.__atcs.lastFrame.t >= 5")
                    assert await frame.inner_text(".pane-title .title") == title
                await frame.click("#btn-stop")
                await frame.wait_for_function("window.__atcs.status === 'stopped'")
                await frame.click("#btn-mode")
                await frame.select_option("#sel-ctrl", "fixed")
                await frame.select_option("#sel-ctrl2", "queue_based")
                await frame.click("#btn-start")
                await frame.wait_for_function("window.__atcs.status === 'running' && window.__atcs.lastFrame && window.__atcs.lastFrame.panes.length === 2 "
                                              "&& window.__atcs.lastFrame.t >= 10", timeout=60000)
                two = await frame.evaluate("window.__atcs.lastFrame.panes.map(p => [p.controller, p.t])")
                assert [c for c, _ in two] == ["fixed", "queue_based"] and two[0][1] == two[1][1]  # lockstep
                assert len(sumo_processes()) == 4  # two workers, two SUMO

                # 7. stop: partial results are real SUMO numbers and nothing keeps running
                await frame.click("#btn-stop")
                await frame.wait_for_function("window.__atcs.status === 'stopped'")
                await frame.click("#btn-results")
                await frame.wait_for_selector("#results:not([hidden])")
                assert "SUMO" in await frame.inner_text("#results-body")
                for _ in range(50):
                    if not sumo_processes():
                        break
                    await pg.wait_for_timeout(100)
                assert sumo_processes() == []

                # 8. the service is closed under the page: it says so and keeps the 3D view in place
            await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service not running'", timeout=15000)
            assert "Lost contact" in await pg.inner_text("#service-detail")
            assert await pg.is_visible("#stage") and not await pg.is_visible("#unavailable")

            # 9. it comes back: the page and the 3D view recover without a reload
            with service_on(port):
                await pg.wait_for_function("document.querySelector('#service-pill').textContent === 'Service running'", timeout=20000)
                await frame.wait_for_function("!document.querySelector('#conn').classList.contains('off')", timeout=20000)
                await frame.wait_for_function("window.__atcs.status === 'idle'", timeout=20000)  # the new service has no run
            assert unexpected(b.errors, EXPECTED_WHEN_DOWN + ("WebSocket",)) == []
    run(go())
    assert sumo_processes() == [], "SUMO processes outlived the service"


def test_closing_the_service_mid_run_leaves_no_sumo_behind(site):
    port = free_port()

    async def go():
        async with Browser() as b:
            pg = b.page
            with service_on(port):
                await pg.goto(f"{site}/simulator3d.html?port={port}")
                frame = await frame_of(pg)
                await settle_idle(frame)
                await frame.select_option("#sel-ctrl", "queue_based")
                await frame.click("#btn-start")
                await frame.wait_for_function("window.__atcs.status === 'running' && window.__atcs.lastFrame && window.__atcs.lastFrame.t >= 3", timeout=60000)
                assert len(sumo_processes()) == 2
            # leaving the `with` stops the service (SIGTERM, like Ctrl+C in its terminal) while a run is active
            deadline = time.time() + 10
            while sumo_processes() and time.time() < deadline:
                await asyncio.sleep(0.1)
            assert sumo_processes() == []
    run(go())


def test_keyboard_shortcuts_and_full_screen_work_inside_the_frame(site):
    """The 3D view's keys (Space, X, H, F) work once the view has focus, which the page gives it when it loads."""
    port = free_port()

    async def go():
        async with Browser() as b:
            pg = b.page
            with service_on(port):
                await pg.goto(f"{site}/simulator3d.html?port={port}")
                frame = await frame_of(pg)
                await settle_idle(frame)
                assert await pg.evaluate("document.activeElement.id") == "stage"  # keys reach the 3D view without a click
                await pg.keyboard.press("h")
                await frame.wait_for_function("document.body.classList.contains('clean')")
                await pg.keyboard.press("h")
                await frame.wait_for_function("!document.body.classList.contains('clean')")
                await pg.keyboard.press("Space")
                await frame.wait_for_function("window.__atcs.status === 'running' && window.__atcs.lastFrame && window.__atcs.lastFrame.t >= 2", timeout=60000)
                await pg.keyboard.press("Space")
                await frame.wait_for_function("window.__atcs.status === 'paused'")
                await pg.keyboard.press("x")
                await frame.wait_for_function("window.__atcs.status === 'stopped'")
                await pg.keyboard.press("f")
                await frame.wait_for_function("document.fullscreenElement !== null", timeout=10000)
                await frame.evaluate("document.exitFullscreen()")
            assert unexpected(b.errors, ("WebSocket",)) == []
    run(go())
