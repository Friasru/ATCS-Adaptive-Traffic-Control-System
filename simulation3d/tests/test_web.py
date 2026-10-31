"""The 3D page shows what SUMO reports and nothing else. Real Chromium (software WebGL), real server, real SUMO."""
import asyncio
import json
import math
import os

import pytest

from atcs3d import paths
from tests.browser_util import Browser
from tests.helpers import ServerProc

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def server():
    with ServerProc() as s:
        yield s


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------------------------------------ basics
def test_page_loads_renders_a_scene_and_has_no_script_errors(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await pg.wait_for_function("window.__atcs.renderer().calls > 20", timeout=20000)
            shot = await pg.screenshot()
            assert len(shot) > 40_000, "canvas looks empty"
            assert b.errors == []
            assert await pg.inner_text("#status-pill") == "Ready"
            names = await pg.evaluate("[...document.querySelectorAll('#sel-ctrl option')].map(o => o.textContent)")
            assert names == ["Fixed timing", "Queue-Based", "Wait-Time"]
            return await pg.evaluate("document.querySelector('#sel-scenario').options.length")
    assert run(go()) == 4


def test_page_works_without_internet(server):
    """Every script/style/model comes from the local server: block all non-local requests and the page still works."""
    async def go():
        async with Browser() as b:
            external = []

            async def block(route):
                url = route.request.url
                if url.startswith(server.url) or url.startswith("data:") or url.startswith("blob:"):
                    await route.continue_()
                else:
                    external.append(url)
                    await route.abort()

            await b.page.route("**/*", block)
            await b.open(server.url)
            await b.start_run(("fixed",), speed=20)
            await b.wait_sim_time(10)
            assert external == []
            assert b.errors == []
    run(go())


# ------------------------------------------------------------------------------------ signals and vehicles
SAMPLER = """
() => new Promise(resolve => {
  const out = []; let last = -1;
  const net = window.__atcs.net();
  const id = setInterval(() => {
    const f = window.__atcs.lastFrame; if (!f || f.seq === last) return; last = f.seq;
    const pane = window.__atcs.pane(0); if (!pane || pane.curT !== f.panes[0].t) return;
    const expect = {};
    for (const a of net.approaches) {
      const ch = f.panes[0].tls.state[a.straight_link];
      expect[a.lane] = 'Gg'.includes(ch) ? 'green' : 'yY'.includes(ch) ? 'yellow' : 'red';
    }
    out.push({t: f.t, state: f.panes[0].tls.state, shown: pane.signalColors, expect});
    if (f.t >= %d) { clearInterval(id); resolve(out); }
  }, 20);
})
"""


@pytest.mark.parametrize("controller", ["fixed", "queue_based"])
def test_signal_heads_always_show_the_sumo_signal_state(server, controller):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run((controller,), speed=20)
            samples = await pg.evaluate(SAMPLER % 140)
            assert len(samples) > 60
            assert all(s["shown"] == s["expect"] for s in samples), [s for s in samples if s["shown"] != s["expect"]][:2]
            seen = {c for s in samples for c in s["shown"].values()}
            assert seen == {"red", "yellow", "green"}, f"expected to see every colour, saw {seen}"
            # the two axes never show green together
            for s in samples:
                shown = s["shown"]
                ns = {shown["top0A0_0"], shown["bottom0A0_0"]}
                ew = {shown["left0A0_0"], shown["right0A0_0"]}
                assert not ("green" in ns and "green" in ew)
            assert b.errors == []
    run(go())


def expected_model(v):
    """Where a model must be, from the SUMO record alone (front-bumper position + heading + length)."""
    a = math.radians(v["a"])
    cx = v["x"] - math.sin(a) * v["len"] / 2
    cy = v["y"] - math.cos(a) * v["len"] / 2
    return cx - 200.0, -(cy - 200.0), math.pi / 2 - a


def test_paused_scene_is_exactly_the_last_sumo_frame(server):
    """After Pause, interpolation has settled: every model sits where SUMO put that vehicle, with the right heading,
    one model per SUMO vehicle id, no extras."""
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run(("queue_based",), speed=20)
            await b.wait_sim_time(75)
            await pg.click("#btn-pause")
            await pg.wait_for_function("window.__atcs.status === 'paused'")
            await pg.wait_for_timeout(1500)
            data = await pg.evaluate("({f: window.__atcs.lastFrame, p: window.__atcs.pane(0)})")
            frame_vehicles = {v["id"]: v for v in data["f"]["panes"][0]["vehicles"]}
            p = data["p"]
            assert p["curT"] == data["f"]["panes"][0]["t"]
            assert sorted(frame_vehicles) == p["vehicleIds"], "rendered ids differ from SUMO ids"
            assert len(p["vehicleIds"]) == len(set(p["vehicleIds"])) >= 10
            assert p["u"] == 1
            for vid, v in frame_vehicles.items():
                ex, ez, eyaw = expected_model(v)
                x, z, yaw = p["vehiclePositions"][vid]
                assert abs(x - ex) < 0.01 and abs(z - ez) < 0.01, (vid, (x, z), (ex, ez))
                dyaw = (yaw - eyaw + math.pi) % (2 * math.pi) - math.pi
                assert abs(dyaw) < 1e-3
                # model forward (+X rotated by yaw about Y) must equal the SUMO heading direction (north = -z)
                fx, fz = math.cos(yaw), -math.sin(yaw)
                hx, hz = math.sin(math.radians(v["a"])), -math.cos(math.radians(v["a"]))
                assert abs(fx - hx) < 1e-3 and abs(fz - hz) < 1e-3
    run(go())


def test_vehicles_drive_on_their_lanes_and_stop_before_the_line(server):
    """Coordinates map correctly onto the road: a rendered vehicle's lane centre line is where SUMO says its lane is."""
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run(("fixed",), speed=20)
            await b.wait_sim_time(60)
            await pg.click("#btn-pause")
            await pg.wait_for_function("window.__atcs.status === 'paused'")
            await pg.wait_for_timeout(1200)
            data = await pg.evaluate("({net: window.__atcs.net(), p: window.__atcs.pane(0)})")
        lanes = {l["id"]: l["shape"] for e in data["net"]["edges"] for l in e["lanes"]}
        checked = 0
        for v in data["p"]["cur"]:
            shape = lanes.get(v["lane"])
            if not shape:
                continue
            (x0, y0), (x1, y1) = shape[0], shape[-1]
            # perpendicular distance from the SUMO position to the lane's centre line
            dx, dy = x1 - x0, y1 - y0
            dist = abs(dx * (y0 - v["y"]) - (x0 - v["x"]) * dy) / math.hypot(dx, dy)
            assert dist < 0.05
            checked += 1
        assert checked >= 8
    run(go())


def test_interpolation_is_exactly_a_blend_of_two_consecutive_sumo_positions(server):
    """While running, each displayed vehicle sits at (1-u)*previous + u*current of two consecutive SUMO snapshots, using
    the very snapshots and u the renderer used for that draw. Smoothing therefore never invents a path."""
    async def go():
        async with Browser(1280, 720) as b:
            pg = await b.open(server.url, "?shadows=0")
            await b.start_run(("fixed",), speed=5)
            await b.wait_sim_time(40)
            res = await pg.evaluate("""() => new Promise(resolve => {
              let n = 0, checked = 0, worst = 0, mid = 0; const bad = [];
              const centre = (v) => { const a = v.a * Math.PI / 180; return [v.x - Math.sin(a) * v.len / 2, v.y - Math.cos(a) * v.len / 2]; };
              const id = setInterval(() => {
                const p = window.__atcs.pane(0); if (!p || !p.used || p.used.prevT === null) return;
                const u = p.used.u, prev = Object.fromEntries(p.used.prev.map(v => [v.id, v]));
                if (p.used.curT - p.used.prevT !== 1) return;
                for (const v of p.used.cur) {
                  const a = prev[v.id]; if (!a) continue;
                  const c = centre(v), d = centre(a), pos = p.vehiclePositions[v.id];
                  const ex = d[0] + (c[0] - d[0]) * u - 200, ez = -((d[1] + (c[1] - d[1]) * u) - 200);
                  const off = Math.hypot(pos[0] - ex, pos[1] - ez);
                  checked++; worst = Math.max(worst, off); if (u > 0.05 && u < 0.95) mid++;
                  if (off > 0.01) bad.push([v.id, off, u]);
                }
                if (++n >= 40) { clearInterval(id); resolve({checked, worst, mid, bad: bad.slice(0, 3)}); }
              }, 25);
            })""")
            assert res["checked"] > 100 and res["mid"] > 20, "need samples between snapshots, not only at them"
            assert res["bad"] == [] and res["worst"] < 0.01
    run(go())


# ----------------------------------------------------------------------------------------------- controls
def test_ui_controls_start_pause_resume_restart_stop_and_controller_switch(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run(("fixed",), speed=20)
            assert await pg.inner_text("#status-pill") == "Running"
            assert await pg.inner_text(".pane-title .title") == "Traditional Fixed Timing"

            await b.wait_sim_time(6)
            await pg.click("#btn-pause")
            await pg.wait_for_function("window.__atcs.status === 'paused'")
            await pg.wait_for_timeout(300)
            t1 = await pg.evaluate("window.__atcs.lastFrame.t")
            await pg.wait_for_timeout(1500)
            assert await pg.evaluate("window.__atcs.lastFrame.t") == t1, "clock moved while paused"
            assert await pg.inner_text("#btn-pause") == "▶ Resume"

            await pg.click("#btn-pause")
            await pg.wait_for_function(f"window.__atcs.lastFrame.t > {t1}")
            assert await pg.inner_text("#status-pill") == "Running"

            rid = await pg.evaluate("window.__atcs.runId")
            await pg.click("#btn-restart")
            await pg.wait_for_function(f"window.__atcs.runId > {rid} && window.__atcs.status === 'running'")
            await pg.wait_for_function("window.__atcs.lastFrame.t < 4")  # fresh clock
            assert await pg.evaluate("window.__atcs.pane(0).vehicleIds.length") < 6

            # choosing another controller during a run ends it and starts a fresh run with that controller
            rid = await pg.evaluate("window.__atcs.runId")
            await pg.select_option("#sel-ctrl", "queue_based")
            await pg.wait_for_function(f"window.__atcs.runId > {rid} && window.__atcs.status === 'running'", timeout=30000)
            await pg.wait_for_function("window.__atcs.lastFrame && window.__atcs.lastFrame.panes[0].controller === 'queue_based'")
            assert await pg.inner_text(".pane-title .title") == "ATCS Adaptive Control · Queue-Based"
            assert await pg.evaluate("window.__atcs.lastFrame.t") < 15

            await pg.click("#btn-stop")
            await pg.wait_for_function("window.__atcs.status === 'stopped'")
            assert await pg.is_enabled("#btn-start") and not await pg.is_enabled("#btn-stop")
            assert b.errors == []
    run(go())


def test_scenario_selection_changes_the_run_and_labels(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await pg.select_option("#sel-scenario", "held_out")
            await pg.wait_for_function("document.querySelector('#scenario-info').textContent.includes('north–south 600 veh/h')")
            await b.start_run(("fixed",), scenario="held_out", speed=20)
            await b.wait_sim_time(120)
            routes = await pg.evaluate("[...new Set(window.__atcs.lastFrame.panes[0].vehicles.map(v => v.route))].sort()")
            assert routes == ["east_west", "north_south", "south_north", "west_east"]
            await pg.click("#btn-stop")
    run(go())


def test_speed_buttons_and_keyboard_shortcuts(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await pg.click("#speeds button[data-speed='2']")
            await pg.wait_for_function("document.querySelector('#speeds button[data-speed=\"2\"]').classList.contains('on')")
            await pg.keyboard.press("Space")  # start
            await pg.wait_for_function("window.__atcs.status === 'running'")
            await pg.keyboard.press("Space")  # pause
            await pg.wait_for_function("window.__atcs.status === 'paused'")
            await pg.keyboard.press("Space")  # resume
            await pg.wait_for_function("window.__atcs.status === 'running'")
            cam0 = await pg.evaluate("window.__atcs.pane(0).camera.pos")
            await pg.keyboard.press("t")
            await pg.wait_for_timeout(1500)
            cam1 = await pg.evaluate("window.__atcs.pane(0).camera.pos")
            assert cam1[1] > 100 and abs(cam1[0]) < 1 and cam1 != cam0  # top-down
            await pg.keyboard.press("d")
            await pg.wait_for_timeout(1500)
            cam2 = await pg.evaluate("window.__atcs.pane(0).camera.pos")
            assert cam2[1] < 5  # street level
            await pg.keyboard.press("x")
            await pg.wait_for_function("window.__atcs.status === 'stopped'")
    run(go())


def test_side_by_side_shows_two_independent_runs_in_lockstep(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run(("fixed", "queue_based"), speed=20)
            await b.wait_sim_time(100)
            titles = await pg.evaluate("[window.__atcs.pane(0).title, window.__atcs.pane(1).title]")
            assert titles == ["Traditional Fixed Timing", "ATCS Adaptive Control · Queue-Based"]
            data = await pg.evaluate("""() => { const f = window.__atcs.lastFrame; return {
                 t: f.panes.map(p => p.t), ctl: f.panes.map(p => p.controller),
                 st: f.panes.map(p => p.tls.state), p0: window.__atcs.pane(0), p1: window.__atcs.pane(1) } }""")
            assert data["t"][0] == data["t"][1]
            assert data["ctl"] == ["fixed", "queue_based"]
            for i in (0, 1):
                pane = data[f"p{i}"]
                assert pane["curT"] == data["t"][i]
            # both views visible, side by side, of equal size
            boxes = await pg.evaluate("[...document.querySelectorAll('.pane')].map(e => { const r = e.getBoundingClientRect(); return [r.left, r.width, r.height]; })")
            assert abs(boxes[0][1] - boxes[1][1]) < 4 and boxes[1][0] > 700
            await pg.click("#btn-stop")
    run(go())


def test_results_panel_shows_the_measured_benchmark(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await pg.select_option("#sel-scenario", "uneven")  # the panel follows the selected scenario
            await pg.wait_for_function("document.querySelector('#scenario-info').textContent.includes('Uneven')")
            await pg.click("#btn-results")
            await pg.wait_for_selector("table.res")
            rows = await pg.evaluate("[...document.querySelectorAll('table.res tr')].slice(1).map(r => [...r.children].map(c => c.textContent))")
            doc = json.load(open(os.path.join(paths.RESULTS_DIR, "benchmark_uneven.json")))
            assert [r[0] for r in rows] == ["Fixed timing", "Queue-Based", "Wait-Time"]
            for row, key in zip(rows, ["fixed", "queue_based", "wait_time"]):
                m = doc["controllers"][key]
                assert row[1] == f"{m['avg_wait_s']:.2f}" and row[2] == f"{m['max_wait_s']:.0f}" and row[4] == str(m["vehicles_completed"])
            assert rows[1][5] == "−54.7%" and rows[0][5] == "(baseline)"
            text = await pg.inner_text("#results")
            assert "not real-world traffic measurements" in text and "seed 23423" in text
    run(go())


def test_reload_recovers_the_running_simulation(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run(("queue_based",), speed=10)
            await b.wait_sim_time(30)
            await pg.reload()
            await pg.wait_for_function("window.__atcs && document.querySelector('#sel-ctrl').options.length > 0")
            await pg.wait_for_function("window.__atcs.status === 'running' && window.__atcs.lastFrame", timeout=20000)
            assert await pg.evaluate("document.querySelector('#sel-ctrl').value") == "queue_based"
            await pg.wait_for_function("window.__atcs.pane(0) && window.__atcs.pane(0).vehicleIds.length > 3", timeout=20000)
            await pg.click("#btn-stop")
            assert b.errors == []
    run(go())


@pytest.mark.slow
def test_results_panel_opens_by_itself_when_a_run_finishes(server):
    """Demo step 4: let the whole simulated hour play (50x) and the measured numbers appear without a click."""
    async def go():
        async with Browser(1280, 720) as b:
            pg = await b.open(server.url, "?shadows=0&dpr=1")
            await b.start_run(("queue_based",), scenario="balanced", speed=50)
            assert await pg.is_hidden("#results")
            await pg.wait_for_function("window.__atcs.status === 'ended'", timeout=150000)
            await pg.wait_for_selector("#results table.res", timeout=10000)
            assert await pg.is_visible("#results")
            text = await pg.inner_text("#results")
            assert "finished" in text and "Queue-Based" in text
            assert await pg.inner_text("#status-pill") == "Finished"
            assert b.errors == []
    run(go())
