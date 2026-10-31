"""The state bridge tells the truth about SUMO, and the controllers obey their own rules.

All of these use the worker exactly as the live server does (one simulated second per request). No browser involved.
"""
import asyncio
import json
import os
import re
import subprocess
import sys

import pytest

from atcs3d import netmodel, paths, scenarios
from tests.helpers import collect, sumo_processes

CONTROLLERS = list(scenarios.CONTROLLERS)
NET = netmodel.load_network()


# ----------------------------------------------------------------------------------------------- bridge == SUMO
@pytest.mark.parametrize("controller", CONTROLLERS)
def test_frames_equal_independent_sumo_recording(controller):
    """Every vehicle id, position, heading, speed, lane and the signal string equal what direct TraCI getters
    report in an independent SUMO+controller run (no bridge code involved), for 400 consecutive seconds."""
    steps = 400
    out = os.path.join(paths.OUTPUT_DIR, f"indep_{controller}", "ref.json")
    subprocess.run([sys.executable, os.path.join(paths.ROOT, "tests", "independent_reference.py"), controller, str(steps), out, "500", "200"],
                   check=True, capture_output=True, timeout=300)
    ref = json.load(open(out))
    frames, _ = asyncio.run(collect(controller, 500, 200, steps, tag=f"bridge_eq_{controller}"))
    frames = frames[1:]  # drop the t=0 snapshot
    assert len(frames) == len(ref) == steps
    max_dx = 0.0
    for f, r in zip(frames, ref):
        assert f["t"] == pytest.approx(r["t"], abs=0.05)
        assert f["tls"]["state"] == r["tls"], f"signal state differs at t={r['t']}"
        got = {v["id"]: v for v in f["vehicles"]}
        assert set(got) == set(r["veh"]), f"vehicle id sets differ at t={r['t']}"
        for vid, (x, y, a, s, lane) in r["veh"].items():
            v = got[vid]
            assert v["lane"] == lane
            max_dx = max(max_dx, abs(v["x"] - x), abs(v["y"] - y))
            assert abs(v["x"] - x) <= 0.0051 and abs(v["y"] - y) <= 0.0051  # frames are rounded to 1 cm
            assert abs(v["a"] - a) <= 0.051 or abs(abs(v["a"] - a) - 360) <= 0.051
            assert abs(v["v"] - s) <= 0.0051
    assert max_dx <= 0.0051


@pytest.mark.parametrize("controller", CONTROLLERS)
def test_runs_are_deterministic(controller):
    """Same controller + same demand + same seed -> identical frames. This is what makes comparisons fair."""
    a, _ = asyncio.run(collect(controller, 500, 200, 300, tag=f"det_a_{controller}"))
    b, _ = asyncio.run(collect(controller, 500, 200, 300, tag=f"det_b_{controller}"))
    assert a == b


def test_all_controllers_see_the_same_demand():
    """Vehicles are inserted at identical times for all controllers until queues differ (first 15 s: no signal effect)."""
    firsts = []
    for c in CONTROLLERS:
        frames, _ = asyncio.run(collect(c, 500, 200, 12, tag=f"demand_{c}"))
        firsts.append([(f["t"], sorted(v["id"] for v in f["vehicles"])) for f in frames])
    assert firsts[0] == firsts[1] == firsts[2]


# ---------------------------------------------------------------------------------------------- full-hour frames
@pytest.fixture(scope="module")
def hour():
    """Complete simulated hour for every controller, rush-hour demand (both axes busy), as streamed by the bridge."""
    async def go():
        res = await asyncio.gather(*(collect(c, 900, 700, None, tag=f"hour_{c}") for c in CONTROLLERS))
        return dict(zip(CONTROLLERS, res))
    return asyncio.run(go())


def phase_runs(frames):
    """[(phase, length_in_seconds)] for every complete phase in the stream (the open first/last runs are dropped)."""
    runs, cur, n = [], None, 0
    for f in frames[1:]:
        p = f["tls"]["phase"]
        if p == cur:
            n += 1
        else:
            if cur is not None:
                runs.append((cur, n))
            cur, n = p, 1
    return runs[1:]  # the very first run started before the stream


@pytest.mark.parametrize("controller", CONTROLLERS)
def test_yellow_is_always_three_seconds_and_phases_cycle(hour, controller):
    runs = phase_runs(hour[controller][0])
    assert len(runs) > 20
    yellow = [n for p, n in runs if p in (1, 3)]
    assert yellow and all(n == 3 for n in yellow)
    seq = [p for p, _ in runs]
    assert all((b - a) % 4 == 1 for a, b in zip(seq, seq[1:])), "phases must go green, yellow, other green, yellow, ..."


def test_fixed_follows_the_programmed_cycle(hour):
    greens = [n for p, n in phase_runs(hour["fixed"][0]) if p in (0, 2)]
    assert greens and all(n == 42 for n in greens)  # tlLogic: 42 s green, 3 s yellow, per direction


def test_queue_based_green_limits(hour):
    paths.use_atcs_source()
    from controllers import queue_based as q  # the ATCS module itself, so the limits are never copied here

    greens = [n for p, n in phase_runs(hour["queue_based"][0]) if p in (0, 2)]
    assert min(greens) >= q.MIN_GREEN
    assert max(greens) <= q.MAX_GREEN + 2  # both axes are busy in this scenario, so the max-green rule applies
    assert max(greens) < 42  # never the fixed programme's 42 s


def test_wait_time_green_limits(hour):
    paths.use_atcs_source()
    from controllers import wait_time as w

    greens = [n for p, n in phase_runs(hour["wait_time"][0]) if p in (0, 2)]
    assert min(greens) >= w.MIN_GREEN
    assert max(greens) <= w.MAX_GREEN + 2
    assert max(greens) < 42


@pytest.mark.parametrize("controller", ["queue_based", "wait_time"])
def test_adaptive_controllers_give_the_busier_axis_longer_green(controller):
    """Lopsided demand (east-west 1100 veh/h, north-south 100): the east-west green stretches, north-south stays short.
    The Fixed programme gives both axes the same 42 s no matter what."""
    frames, _ = asyncio.run(collect(controller, 1100, 100, None, tag=f"adapt_{controller}"))
    runs = phase_runs(frames)
    ew = [n for p, n in runs if p == 2]
    ns = [n for p, n in runs if p == 0]
    assert sum(ew) / len(ew) > sum(ns) / len(ns) + 3
    assert max(ew) >= 30
    assert max(ns) <= 25


# ------------------------------------------------------------------------------------- vehicles vs. signal heads
def internal_link(lane):
    m = re.match(r":A0_(\d+)_\d+$", lane)
    return int(m.group(1)) if m else None


@pytest.mark.parametrize("controller", CONTROLLERS)
def test_no_vehicle_enters_the_junction_on_red_and_head_mapping_is_right(hour, controller):
    """For every vehicle that crosses the stop line, the signal char for its own link must not be red in the frame
    that reports the crossing, and that link is the straight-link our signal head for its approach displays."""
    frames = hour[controller][0]
    approach_by_edge = {a["edge"]: a for a in NET["approaches"]}
    prev_lane, crossings, bad = {}, 0, []
    for f in frames:
        for v in f["vehicles"]:
            lane, last = v["lane"], prev_lane.get(v["id"])
            link = internal_link(lane)
            if link is not None and last is not None and internal_link(last) is None:
                crossings += 1
                ch = f["tls"]["state"][link]
                if ch in "rR":
                    bad.append((f["t"], v["id"], link))
                app = approach_by_edge[last.rsplit("_", 1)[0]]
                assert link == app["straight_link"], "signal head for this approach shows a different link than the cars use"
            prev_lane[v["id"]] = lane
    assert crossings > 1500
    assert not bad, f"vehicles entered on red: {bad[:5]}"


@pytest.mark.parametrize("controller", CONTROLLERS)
def test_vehicles_wait_at_the_line_only_because_of_the_signal(hour, controller):
    """Cars do stop at the line (so the red/yellow state is what holds them), and none stands still at the line
    under a green longer than the start-up lag, so a green head always means 'go'."""
    frames = hour[controller][0]
    approach_by_lane = {a["lane"]: a for a in NET["approaches"]}
    held_on_red = 0
    green_streak, worst_streak = {}, 0
    for f in frames:
        for v in f["vehicles"]:
            a = approach_by_lane.get(v["lane"])
            if not a:
                continue
            qx, qy = a["stop_point"]
            at_line = ((v["x"] - qx) ** 2 + (v["y"] - qy) ** 2) ** 0.5 < 1.6 and v["v"] < 0.1  # SUMO stops the front bumper ~1.0 m before the lane end
            ch = f["tls"]["state"][a["straight_link"]]
            if at_line and ch in "rRyY":
                held_on_red += 1
            if at_line and ch in "Gg":
                green_streak[v["id"]] = green_streak.get(v["id"], 0) + 1
                worst_streak = max(worst_streak, green_streak[v["id"]])
            else:
                green_streak.pop(v["id"], None)
    assert held_on_red > 200, "cars should visibly wait at the stop line during red"
    assert worst_streak <= 4, f"a car stood at the line for {worst_streak} s on green"


# ------------------------------------------------------------------------------------------ results are the originals'
@pytest.mark.parametrize("controller", CONTROLLERS)
def test_full_hour_matches_published_atcs_figures_for_uneven_traffic(controller):
    published = json.load(open(os.path.expanduser("~/ATCS-Adaptive-Traffic-Control-System/results/results.json")))
    entry = published["scenarios"][0]  # uneven 500/200
    _, res = asyncio.run(collect(controller, 500, 200, None, tag=f"pub_{controller}"))
    m = res["metrics"]
    assert m["vehicles_completed"] == 1400
    if controller == "fixed":
        ref = entry["fixed"]
    elif controller == "queue_based":
        ref = entry["adaptive"]  # ATCS publishes queue-based as "adaptive"
    else:
        ref = None
    if ref:
        for k in ("avg_wait_s", "max_wait_s", "avg_time_loss_s", "avg_trip_duration_s"):
            assert round(m[k], 2) == ref[k]
    else:  # wait-time: figures in the ATCS README / tuning sweep (training scenario "uneven": time loss 15.35)
        assert round(m["avg_time_loss_s"], 2) == 15.35


def test_no_sumo_process_left_behind():
    assert sumo_processes() == []


# ------------------------------------------------------------------------------------------------ turning traffic
def test_turning_vehicles_are_reported_correctly(tmp_path):
    """The ATCS scenarios only go straight, but SUMO can turn and the bridge must report it faithfully. Diagnostic demand
    (not an ATCS scenario) with left and right turns from the west arm and a right turn from the north arm."""
    routes = tmp_path / "turns.rou.xml"
    routes.write_text(
        "<routes>\n"
        '<route id="w_n" edges="left0A0 A0top0"/><route id="w_s" edges="left0A0 A0bottom0"/>'
        '<route id="n_w" edges="top0A0 A0left0"/><route id="w_e" edges="left0A0 A0right0"/>\n'
        '<flow id="f_wn" route="w_n" begin="0" end="300" vehsPerHour="240"/>'
        '<flow id="f_ws" route="w_s" begin="0" end="300" vehsPerHour="240"/>'
        '<flow id="f_nw" route="n_w" begin="0" end="300" vehsPerHour="200"/>'
        '<flow id="f_we" route="w_e" begin="0" end="300" vehsPerHour="240"/>\n</routes>\n')
    frames, _ = asyncio.run(collect("queue_based", 0, 0, 200, tag="turns", routes=str(routes)))
    by_id = {}
    for f in frames:
        for v in f["vehicles"]:
            by_id.setdefault(v["id"], []).append((f["t"], v))
    turning = {vid: tr for vid, tr in by_id.items() if any(internal_link(v["lane"]) in (12, 14) or v["route"] in ("w_n", "w_s", "n_w") for _, v in tr)}
    assert len(turning) >= 8
    seen_lanes = {v["lane"] for tr in by_id.values() for _, v in tr}
    assert {":A0_14_0", ":A0_22_0"} & seen_lanes or {":A0_14", ":A0_22"} & {l.rsplit("_", 1)[0] for l in seen_lanes}, "left turn never seen on its internal lane"
    completed = 0
    for vid, tr in turning.items():
        angles = [v["a"] for _, v in tr]
        steps = [abs((b - a + 180) % 360 - 180) for a, b in zip(angles, angles[1:])]
        assert max(steps, default=0) <= 100, f"{vid} turned {max(steps)} degrees in one second"  # a turn is < 90 deg + rounding
        route = tr[0][1]["route"]
        out_edge, expected = {"w_n": ("A0top0", 0.0), "w_s": ("A0bottom0", 180.0), "n_w": ("A0left0", 270.0), "w_e": ("A0right0", 90.0)}[route]
        if sum(v["lane"].startswith(out_edge) for _, v in tr) >= 3:  # a few seconds on the outgoing road: body fully straightened
            completed += 1
            assert abs((angles[-1] - expected + 180) % 360 - 180) < 1.0
    assert completed >= 5
    # headings of turning vehicles really do change: at least one vehicle changed heading by 60+ degrees overall
    assert any(abs((tr[-1][1]["a"] - tr[0][1]["a"] + 180) % 360 - 180) > 60 for tr in turning.values())


# ------------------------------------------------------------------------------------- live estimate vs. final figure
def test_live_average_wait_estimate_is_close_to_the_final_sumo_figure():
    """The HUD's 'avg wait so far' is a running estimate; at the end of the run it must be within 0.1 s of SUMO's tripinfo."""
    frames, res = asyncio.run(collect("queue_based", 500, 200, None, tag="live_est"))
    live = frames[-1]["live"]["avg_wait"]
    final = res["metrics"]["avg_wait_s"]
    assert live == pytest.approx(final, abs=0.1), (live, final)
