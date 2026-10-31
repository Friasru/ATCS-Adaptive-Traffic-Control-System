"""The numbers on the HUD mean what their labels say.

Regression tests for the "counter goes up when a car appears, down as it drives, up again at the light" report. Cause:
SUMO inserts every vehicle with speed 0, and its halting definition (speed < 0.1 m/s) counts it during the second it is
placed, although it has not stopped anywhere. The HUD's `stopped` leaves out exactly those vehicles for that one second;
`halt` (the Queue-Based controller's input) is untouched. See docs/COUNTER_INVESTIGATION.md.

Worker-level tests drive the real SUMO exactly as the live server does; the last two go through the server's WebSocket
(side-by-side) and a real browser (what is actually printed on the page).
"""
import asyncio
import os
import time
import xml.etree.ElementTree as ET

import pytest

from atcs3d import paths, scenarios
from tests.helpers import ServerProc, WsClient, assert_counter_invariants, collect

CONTROLLERS = list(scenarios.CONTROLLERS)


def run(coro):
    return asyncio.run(coro)


def raw_halt(f):
    return sum(d["halt"] for d in f["lanes"].values())


# --------------------------------------------------------------------------------------------------------------- spawning
def test_a_car_placed_on_the_road_is_not_a_stopped_car():
    """The reported case, on the seeded demo run. Cars appear at t=1, 9, 16, 19 far from the junction at speed 0: SUMO's own
    halting number jumps then, the HUD's `stopped` stays 0 until a car really stops at the red light."""
    frames, _ = run(collect("fixed", 500, 200, 60, tag="cnt_spawn"))
    assert_counter_invariants(frames)
    seen, spawn_seconds = set(), []
    for f in frames[1:]:
        fresh = {v["id"] for v in f["vehicles"]} - seen
        seen |= fresh
        if fresh:
            spawn_seconds.append(f["t"])
    first = frames[1]
    assert first["t"] == 1.0 and first["counts"]["departed"] == 4 and first["counts"]["in_network"] == 4
    assert raw_halt(first) == 4, "SUMO's lane halting number counts the four cars placed this second (this is what used to be displayed)"
    assert first["counts"]["stopped"] == 0 and all(d["stopped"] == 0 for d in first["lanes"].values())
    assert all(g["stopped"] == 0 for g in first["groups"])

    first_stop = min(f["t"] for f in frames if any(v["w"] > 0 for v in f["vehicles"]))
    assert first_stop >= 15, "a car needs ~13 s to drive the 190 m to the stop line; nothing can be waiting before that"
    spawns_before = [t for t in spawn_seconds if t < first_stop]
    assert len(spawns_before) >= 3
    for f in frames:
        if f["t"] < first_stop:
            assert f["counts"]["stopped"] == 0, f"t={f['t']}: a stopped car reported before any car stopped"
    assert all(raw_halt(f) > 0 for f in frames if f["t"] in spawns_before), "the raw SUMO number does blip on spawn seconds"
    stop_frame = next(f for f in frames if f["t"] == first_stop)
    assert stop_frame["counts"]["stopped"] >= 1


def test_a_car_placed_behind_a_real_queue_adds_nothing_until_it_has_stopped_too():
    frames, _ = run(collect("fixed", 500, 200, 60, tag="cnt_behind_queue"))
    seen, hit = set(), 0
    for f in frames[1:]:
        fresh = {v["id"] for v in f["vehicles"]} - seen
        seen |= fresh
        for lane, d in f["lanes"].items():
            placed = [v for v in f["vehicles"] if v["id"] in fresh and v["lane"] == lane]
            waiting = [v for v in f["vehicles"] if v["lane"] == lane and v["w"] > 0]
            if placed and waiting:
                hit += 1
                assert d["halt"] == len(waiting) + len(placed) and d["stopped"] == len(waiting)
    assert hit >= 1, "the scenario never placed a car behind a waiting one: test no longer exercises the case"


# ---------------------------------------------------------------------------------------- moving / stopping / completing
def test_one_vehicle_from_spawn_to_completion(tmp_path):
    """The lifecycle with nothing else on the road: a single west->east car. The fixed program starts with north-south green, so
    it drives up to a red light, waits, goes, and leaves. Every counter is checked in every phase."""
    routes = tmp_path / "solo.rou.xml"
    routes.write_text('<routes>\n<route id="we" edges="left0A0 A0right0"/>\n<vehicle id="solo" route="we" depart="0"/>\n</routes>\n')
    frames, res = run(collect("fixed", 0, 0, None, tag="cnt_solo", routes=str(routes)))
    assert_counter_invariants(frames)

    on_road = [i for i, f in enumerate(frames) if any(v["id"] == "solo" for v in f["vehicles"])]
    assert on_road == list(range(on_road[0], on_road[-1] + 1)), "the car must be on the road for one contiguous stretch"
    spawn, last = on_road[0], on_road[-1]
    me = lambda i: next(v for v in frames[i]["vehicles"] if v["id"] == "solo")  # noqa: E731
    standing = [i for i in on_road if i > spawn and me(i)["v"] < 0.1]
    assert standing, "the car never stopped: the red light did not hold it"
    assert standing == list(range(standing[0], standing[-1] + 1)), "exactly one stop"
    lane = me(standing[0])["lane"]

    # spawn: placed at rest, counted by SUMO's raw number, not by the HUD
    f = frames[spawn]
    assert frames[spawn - 1]["counts"] == {"departed": 0, "arrived": 0, "in_network": 0, "stopped": 0}
    assert me(spawn)["v"] == 0 and me(spawn)["w"] == 0
    assert (f["counts"]["departed"], f["counts"]["in_network"], f["counts"]["arrived"]) == (1, 1, 0)
    assert f["lanes"][lane]["halt"] == 1 and f["lanes"][lane]["stopped"] == 0 and f["counts"]["stopped"] == 0

    # moving: driving towards the junction
    moving = list(range(spawn + 1, standing[0]))
    assert len(moving) >= 10
    for i in moving:
        c = frames[i]["counts"]
        assert me(i)["v"] >= 0.1 and raw_halt(frames[i]) == 0
        assert (c["departed"], c["in_network"], c["arrived"], c["stopped"]) == (1, 1, 0, 0), f"t={frames[i]['t']}"

    # stopping: from the first second below 0.1 m/s until it moves again
    assert len(standing) >= 10
    for k, i in enumerate(standing, start=1):
        c = frames[i]["counts"]
        assert c["stopped"] == 1 and frames[i]["lanes"][lane]["stopped"] == 1 and frames[i]["lanes"][lane]["halt"] == 1
        assert me(i)["w"] == pytest.approx(k, abs=0.01), "SUMO's waiting time runs from the first standing second"
        assert (c["departed"], c["in_network"], c["arrived"]) == (1, 1, 0)

    # released: going again; stopped goes back down while the car is still on the road
    after = list(range(standing[-1] + 1, last + 1))
    assert len(after) >= 10
    for i in after:
        c = frames[i]["counts"]
        assert c["stopped"] == 0 and (c["departed"], c["in_network"], c["arrived"]) == (1, 1, 0)

    # completing: gone from the road, counted as completed, and the final SUMO record agrees
    done = frames[last + 1]["counts"]
    assert done == {"departed": 1, "arrived": 1, "in_network": 0, "stopped": 0}
    assert frames[-1].get("ended") and frames[-1]["counts"] == done
    assert res["metrics"]["vehicles_completed"] == 1

    def collapsed(key):
        out = []
        for f in frames:
            if not out or out[-1] != f["counts"][key]:
                out.append(f["counts"][key])
        return out
    assert collapsed("stopped") == [0, 1, 0], "stopped: no blip at spawn, one rise at the light, one fall when it goes"
    assert collapsed("in_network") == [0, 1, 0]
    assert collapsed("departed") == [0, 1] and collapsed("arrived") == [0, 1]


@pytest.mark.parametrize("controller", CONTROLLERS)
def test_counters_obey_their_definitions_in_every_frame_of_dense_traffic(controller):
    """Rush hour: queues build in all four directions, cars complete, cars keep spawning behind queues."""
    frames, _ = run(collect(controller, 900, 700, 400, tag=f"cnt_dense_{controller}"))
    assert_counter_invariants(frames)
    assert frames[-1]["counts"]["arrived"] > 20 and frames[-1]["counts"]["stopped"] > 0


def test_instantaneous_counters_go_both_ways_and_cumulative_ones_only_up():
    frames, _ = run(collect("fixed", 500, 200, 300, tag="cnt_shape"))
    series = lambda key: [f["counts"][key] for f in frames]  # noqa: E731
    for key in ("departed", "arrived"):
        s = series(key)
        assert all(b >= a for a, b in zip(s, s[1:])), f"{key} decreased"
        assert s[-1] > s[0]
    for key in ("in_network", "stopped"):
        s = series(key)
        assert any(b > a for a, b in zip(s, s[1:])) and any(b < a for a, b in zip(s, s[1:])), f"{key} must be able to rise and fall"
    stopped = series("stopped")
    peak = max(stopped)
    assert peak >= 5
    assert min(stopped[stopped.index(peak):]) < peak, "the queue count must come back down after the queue discharges"


def test_free_flowing_traffic_reports_no_stopped_vehicles():
    """Whenever every car that is not brand new is moving at 0.5 m/s or more, nobody is stopped, in any direction."""
    for controller, ew, ns in (("fixed", 300, 300), ("queue_based", 900, 700), ("wait_time", 200, 600)):
        frames, _ = run(collect(controller, ew, ns, 120, tag=f"cnt_free_{controller}"))
        seen, free_frames = set(), 0
        for f in frames:
            fresh = {v["id"] for v in f["vehicles"]} - seen
            seen |= fresh
            if all(v["v"] >= 0.5 for v in f["vehicles"] if v["id"] not in fresh):
                free_frames += 1
                assert f["counts"]["stopped"] == 0 and all(d["stopped"] == 0 for d in f["lanes"].values()), f"t={f['t']} {controller}"
        assert free_frames >= 10


# --------------------------------------------------------------------------- the raw number is SUMO's own, and is kept
def test_raw_halting_is_sumos_own_number_and_does_include_the_insertion_second():
    """`halt` (the controllers' input) must stay exactly SUMO's figure: compared with the `halting` column of the summary file SUMO
    itself writes, second by second. That figure includes cars placed this second, which is why the HUD does not show it."""
    tag = "cnt_raw_vs_summary"
    frames, _ = run(collect("fixed", 500, 200, 120, tag=tag))
    rows = ET.parse(os.path.join(paths.OUTPUT_DIR, tag, "summary.xml")).getroot().findall("step")
    # a summary row is labelled with the time its step starts: row time=k is the step the bridge reports as t=k+1
    assert len(rows) >= len(frames) - 1
    for f, row in zip(frames[1:], rows):
        assert float(row.attrib["time"]) + 1 == pytest.approx(f["t"])
        assert int(row.attrib["halting"]) == raw_halt(f), f"t={f['t']}"
    assert int(rows[0].attrib["halting"]) == 4 and frames[1]["counts"]["stopped"] == 0


# ------------------------------------------------------------------------------------------ side by side (server level)
@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with ServerProc(results_dir=str(tmp_path_factory.mktemp("results"))) as s:
        yield s


def test_both_panes_of_a_side_by_side_run_use_the_same_counter_definitions(server):
    async def go():
        async with WsClient(server.url) as c:
            await c.idle()
            await c.send("select", controllers=["fixed", "queue_based"], scenario="uneven")
            await c.send("speed", value=50)
            await c.send("start")
            st = await c.until(lambda m: m["type"] == "state" and m["status"] == "running", 30)
            out, end = [], time.time() + 90
            while time.time() < end:
                m = await c.recv(timeout=10)
                if m["type"] == "frame" and m["run_id"] == st["run_id"]:
                    out.append(m)
                    if m["t"] >= 120:
                        break
            await c.idle()
            return out
    frames = run(go())
    assert frames[-1]["t"] >= 120
    for i in (0, 1):
        assert_counter_invariants([f["panes"][i] for f in frames])
    for f in frames:
        a, b = f["panes"]
        assert set(a["counts"]) == set(b["counts"]) == {"departed", "arrived", "in_network", "stopped"}
        assert set(a["lanes"]) == set(b["lanes"]) and all(set(a["lanes"][k]) == set(b["lanes"][k]) for k in a["lanes"])
        assert [set(g) for g in a["groups"]] == [set(g) for g in b["groups"]]
        if f["t"] < 20:  # same demand and, before the adaptive controller's minimum green, the same signals: identical numbers
            assert a["counts"] == b["counts"] and a["lanes"] == b["lanes"], f"t={f['t']}"
    a, b = frames[-1]["panes"]
    assert a["counts"]["stopped"] != b["counts"]["stopped"] or a["counts"]["arrived"] != b["counts"]["arrived"], "the two controllers should have diverged by t=120"
