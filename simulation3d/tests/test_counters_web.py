"""What the page actually prints for each counter, in a real Chromium against the real server and SUMO.

The reported case (a number that rises when a car appears, falls as it drives and rises again at the light) and the
side-by-side comparison, where both panes must use the same definitions. See docs/COUNTER_INVESTIGATION.md.
"""
import asyncio

import pytest

from tests.browser_util import Browser
from tests.helpers import ServerProc

pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(scope="module")
def server():
    with ServerProc() as s:
        yield s


def run(coro):
    return asyncio.run(coro)


# One read of everything on screen plus the frame it was built from, in a single JS task so they cannot disagree.
READ = """() => {
  const a = window.__atcs, f = a.lastFrame; if (!f) return null;
  const panes = [...document.querySelectorAll('.pane:not([hidden])')];
  return {
    t: f.t, seq: f.seq, run_id: f.run_id, runId: a.runId,
    shown: panes.map((el, i) => ({
      groups: [...el.querySelectorAll('.mgroup')].map(g => ({ scope: g.dataset.scope, title: g.querySelector('.mhead').textContent,
                                                                keys: [...g.querySelectorAll('.metric')].map(m => m.dataset.metric) })),
      tiles: Object.fromEntries([...el.querySelectorAll('.metric')].map(m => [m.dataset.metric, m.querySelector('.v').textContent])),
      labels: [...el.querySelectorAll('.metric .k')].map(k => k.textContent),
      board: [...el.querySelectorAll('.signal-board small')].map(s => Number(s.dataset.stopped)),
      badges: [...el.querySelectorAll('.qbadge')].filter(b => !b.classList.contains('hide') && b.style.display !== 'none').map(b => Number(b.textContent)).sort(),
      curT: a.pane(i) ? a.pane(i).curT : null,
    })),
    frame: f.panes.map(p => ({ t: p.t, counts: p.counts,
                               halt: Object.values(p.lanes).reduce((s, l) => s + l.halt, 0),
                               lanes: Object.values(p.lanes).map(l => l.stopped).sort(),
                               groups: p.groups.map(g => g.stopped) })),
  };
}"""


async def read(pg):
    return await pg.evaluate(READ)


async def pause_and_settle(pg):
    """Pause, wait until the last frame has been drawn (tiles, badges, signal rows), and read the screen."""
    await pg.click("#btn-pause")
    await pg.wait_for_function("window.__atcs.status === 'paused'")
    await asyncio.sleep(0.5)
    await pg.wait_for_function("(() => { const f = window.__atcs.lastFrame; return f.panes.every((p, i) => window.__atcs.pane(i).curT === p.t); })()")
    await pg.evaluate("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")
    return await read(pg)


def assert_screen_equals_frame(r):
    """Every tile, badge and signal-row number equals what the frame says, for every pane that is on screen."""
    for i, (s, fr) in enumerate(zip(r["shown"], r["frame"])):
        c = fr["counts"]
        assert s["tiles"]["active"] == str(c["in_network"]), (i, r["t"])
        assert s["tiles"]["stopped"] == str(c["stopped"]), (i, r["t"])
        assert s["tiles"]["spawned"] == str(c["departed"]), (i, r["t"])
        assert s["tiles"]["completed"] == str(c["arrived"]), (i, r["t"])
        assert int(s["tiles"]["active"]) == int(s["tiles"]["spawned"]) - int(s["tiles"]["completed"]), "active must be spawned - completed"
        assert s["board"] == fr["groups"], (i, r["t"])


def assert_badges_are_the_lane_queues(r):
    for i, (s, fr) in enumerate(zip(r["shown"], r["frame"])):
        assert s["badges"] == [n for n in fr["lanes"] if n > 0], f"pane {i} t={r['t']}: badges {s['badges']} vs lane queues {fr['lanes']}"


def test_the_reported_case_a_new_car_does_not_show_up_as_a_stopped_car(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run(("fixed",), scenario="uneven", speed=1)
            await pg.wait_for_function("window.__atcs.lastFrame.run_id === window.__atcs.runId && window.__atcs.lastFrame.t >= 1")
            at_spawn = await pause_and_settle(pg)

            await pg.click(f"#speeds button[data-speed='10']")
            await pg.click("#btn-pause")  # resume
            await pg.wait_for_function("window.__atcs.lastFrame.t >= 40", timeout=60000)
            queued = await pause_and_settle(pg)
            assert b.errors == []
            return at_spawn, queued
    at_spawn, queued = run(go())

    # t=1: four cars have just been placed at the far ends of the approaches. SUMO's halting number says 4; nobody has stopped.
    assert at_spawn["t"] == 1.0, "paused on the spawn second"
    assert at_spawn["frame"][0]["halt"] == 4, "the underlying SUMO halting number does jump at spawn: this is the case under test"
    s = at_spawn["shown"][0]
    assert (s["tiles"]["active"], s["tiles"]["spawned"], s["tiles"]["completed"], s["tiles"]["stopped"]) == ("4", "4", "0", "0")
    assert s["badges"] == [] and s["board"] == [0, 0]
    assert_screen_equals_frame(at_spawn)

    # t>=40: cars really are waiting at the red light; the same tiles, badges and signal rows now show them
    assert queued["t"] >= 40
    assert queued["frame"][0]["counts"]["stopped"] >= 4
    assert_screen_equals_frame(queued)
    assert_badges_are_the_lane_queues(queued)
    assert sum(queued["shown"][0]["badges"]) == queued["frame"][0]["counts"]["stopped"], "queue badges add up to the stopped tile"


def test_side_by_side_both_panes_show_the_same_metrics_defined_the_same_way(server):
    async def go():
        async with Browser() as b:
            pg = await b.open(server.url)
            await b.start_run(("fixed", "queue_based"), scenario="uneven", speed=10)
            samples, last_seq = [], None
            while True:
                r = await read(pg)
                if r and r["run_id"] == r["runId"] and r["seq"] != last_seq and all(s["curT"] == r["t"] for s in r["shown"]):
                    last_seq = r["seq"]
                    samples.append(r)
                    if r["t"] >= 60:
                        break
                await asyncio.sleep(0.03)
            settled = await pause_and_settle(pg)
            await pg.click("#btn-stop")
            assert b.errors == []
            return samples, settled
    samples, settled = run(go())

    assert len(samples) >= 25
    for r in samples:
        assert len(r["shown"]) == 2
        a, c = r["shown"]
        assert a["groups"] == c["groups"] and a["labels"] == c["labels"], "the two panes must show the same metrics with the same labels"
        assert_screen_equals_frame(r)
    assert [g["scope"] for g in samples[0]["shown"][0]["groups"]] == ["now", "total", "wait"]
    assert [g["title"] for g in samples[0]["shown"][0]["groups"]] == ["Right now", "Total since start", "Wait of completed trips"]

    for i in (0, 1):
        for key in ("spawned", "completed"):
            series = [int(r["shown"][i]["tiles"][key]) for r in samples]
            assert all(y >= x for x, y in zip(series, series[1:])), f"pane {i}: cumulative '{key}' went down: {series}"
        for key in ("active", "stopped"):
            series = [int(r["shown"][i]["tiles"][key]) for r in samples]
            assert any(y > x for x, y in zip(series, series[1:])), f"pane {i}: '{key}' never rose"
        stopped = [int(r["shown"][i]["tiles"]["stopped"]) for r in samples]
        assert all(v == 0 for r, v in zip(samples, stopped) if r["t"] < 15), f"pane {i}: stopped shown before any car could have reached the light"

    assert_screen_equals_frame(settled)
    assert_badges_are_the_lane_queues(settled)
