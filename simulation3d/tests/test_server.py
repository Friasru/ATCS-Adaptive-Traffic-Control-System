"""The live server: run control, state consistency and process hygiene, over the real WebSocket protocol."""
import asyncio
import json
import os
import time

import pytest

from atcs3d import paths, scenarios
from tests.helpers import ServerProc, WsClient, collect, sumo_processes


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with ServerProc(results_dir=str(tmp_path_factory.mktemp("results"))) as s:
        yield s


def run(coro):
    return asyncio.run(coro)


async def start(c, controllers=("fixed",), scenario="uneven", speed=20):
    await c.idle()
    await c.send("select", controllers=list(controllers), scenario=scenario)
    await c.send("speed", value=speed)
    await c.send("start")
    st = await c.until(lambda m: m["type"] == "state" and m["status"] == "running", 30)
    return st["run_id"]


async def frames_until(c, t, run_id, timeout=60):
    out = []
    end = time.time() + timeout
    while time.time() < end:
        m = await c.recv(timeout=10)
        if m["type"] == "frame" and m["run_id"] == run_id:
            out.append(m)
            if m["t"] >= t:
                return out
    raise TimeoutError


def test_stream_starts_at_t0_and_advances_one_second_per_frame(server):
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c)
            fr = await frames_until(c, 30, rid)
            assert fr[0]["t"] == 0.0 and fr[0]["panes"][0]["vehicles"] == []
            assert [f["t"] for f in fr] == [float(i) for i in range(len(fr))]
            assert [f["seq"] for f in fr] == list(range(len(fr)))
            await c.idle()
    run(go())


def test_pause_freezes_simulation_time_and_resume_continues_exactly(server):
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c)
            await frames_until(c, 8, rid)
            await c.send("pause")
            await c.until_state("paused")
            late = []
            try:  # at most the step already in flight may still arrive
                while True:
                    m = await c.recv(timeout=1.5)
                    if m["type"] == "frame":
                        late.append(m)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            assert len(late) <= 1
            frozen_t = late[-1]["t"] if late else None
            await asyncio.sleep(1.0)  # still paused: nothing may arrive
            await c.send("resume")
            nxt = await c.until(lambda m: m["type"] == "frame" and m["run_id"] == rid, 10)
            if frozen_t is not None:
                assert nxt["t"] == frozen_t + 1
            await c.idle()
    run(go())


def test_restart_gives_a_fresh_run_with_no_stale_frames(server):
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c)
            await frames_until(c, 12, rid)
            await c.send("restart")
            seen_new_state = False
            first_new_frame = None
            end = time.time() + 30
            while time.time() < end and first_new_frame is None:
                m = await c.recv(timeout=10)
                if m["type"] == "state" and m["run_id"] == rid + 1:
                    seen_new_state = True
                if m["type"] == "frame":
                    if seen_new_state:
                        assert m["run_id"] == rid + 1, "a frame of the old run arrived after the new run was announced"
                        first_new_frame = m
            assert first_new_frame["t"] == 0.0 and first_new_frame["panes"][0]["vehicles"] == []
            await c.idle()
    run(go())


def test_stop_ends_run_reports_partial_results_and_leaves_no_processes(server):
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c, speed=50)
            await frames_until(c, 150, rid)
            assert sumo_processes()
            await c.send("stop")
            res = await c.until(lambda m: m["type"] == "result", 15)
            assert res["complete"] is False and res["run_id"] == rid
            m = res["panes"][0]["metrics"]
            assert m["vehicles_completed"] > 0 and res["panes"][0]["complete"] is False
            await c.until_state("stopped")
        for _ in range(30):
            if not sumo_processes():
                break
            await asyncio.sleep(0.1)
        assert sumo_processes() == []
    run(go())


def test_changing_controller_ends_the_run_explicitly(server):
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c, ("fixed",))
            await frames_until(c, 5, rid)
            n_before = len(c.log)
            await c.send("select", controllers=["queue_based"], scenario="uneven")
            st = await c.until(lambda m: m["type"] == "state" and m["status"] == "idle", 15)
            assert st["controllers"] == ["queue_based"] and st["run_id"] is None
            stopped = [m for m in c.log[n_before:] if m["type"] == "state" and m["status"] == "stopped"]
            assert stopped, "the active run must be reported as stopped, not silently replaced"
            await c.send("start")
            st = await c.until_state("running")
            fr = await frames_until(c, 3, st["run_id"])
            assert {p["controller"] for f in fr for p in f["panes"]} == {"queue_based"}
            await c.idle()
    run(go())


def test_side_by_side_runs_two_sumo_instances_in_lockstep_without_changing_either(server):
    async def go():
        single, _ = await collect("fixed", 500, 200, 90, tag="sbs_single")
        async with WsClient(server.url) as c:
            rid = await start(c, ("fixed", "queue_based"))
            fr = await frames_until(c, 90, rid)
            await c.idle()
        assert all(len(f["panes"]) == 2 for f in fr)
        assert [f["panes"][0]["controller"] for f in fr[:1]] == ["fixed"] and fr[0]["panes"][1]["controller"] == "queue_based"
        assert all(f["panes"][0]["t"] == f["panes"][1]["t"] == f["t"] for f in fr)
        for f, s in zip(fr, single):  # the Fixed side is identical to a stand-alone Fixed run
            p = {k: v for k, v in f["panes"][0].items() if k not in ("controller", "ended")}
            ref = {k: v for k, v in s.items() if k not in ("type",)}
            assert p == ref
        sig = [(f["panes"][0]["tls"]["state"], f["panes"][1]["tls"]["state"]) for f in fr]
        assert any(a != b for a, b in sig), "the two controllers should be seen making different signal decisions"
    run(go())


def test_unknown_commands_are_rejected_and_cannot_touch_signals(server):
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c)
            await frames_until(c, 3, rid)
            await c.send("setPhase", phase=2, tls="A0")
            await c.send("setProgram", program="x")
            errs = []
            fr = []
            end = time.time() + 6
            while time.time() < end and len(fr) < 10:
                m = await c.recv(timeout=5)
                if m["type"] == "error":
                    errs.append(m)
                if m["type"] == "frame":
                    fr.append(m)
            assert len(errs) == 2 and "unknown command" in errs[0]["message"]
            # fixed programme untouched: green until t=42
            tls = [f["panes"][0]["tls"] for f in fr]
            assert all(t["phase"] == 0 for t in tls)
            await c.idle()
    run(go())


def test_last_browser_leaving_pauses_and_a_quick_reconnect_resumes(server):
    async def go():
        async with WsClient(server.url) as a:
            rid = await start(a)
            fr = await frames_until(a, 6, rid)
            t_left = fr[-1]["t"]
        await asyncio.sleep(2.0)  # at 20x an unattended run would advance ~40 s
        async with WsClient(server.url) as b:
            st = await b.until(lambda m: m["type"] == "state", 5)
            first = await b.until(lambda m: m["type"] == "frame" and m["run_id"] == rid, 10)
            assert first["t"] - t_left <= 3, "run kept going with nobody watching"
            assert st["status"] in ("running", "paused")
            await b.idle()
    run(go())


@pytest.mark.parametrize("speed", [5, 10])
def test_pacing_matches_the_selected_speed(server, speed):
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c, speed=speed)
            await frames_until(c, 3, rid)
            t0, n0 = time.time(), 3
            fr = await frames_until(c, 3 + 3 * speed, rid)
            dt = time.time() - t0
            sim = fr[-1]["t"] - n0
            await c.idle()
            return sim / dt
    rate = run(go())
    assert rate == pytest.approx(speed, rel=0.15)


def test_cross_origin_websocket_is_refused(server):
    import aiohttp

    async def go():
        async with aiohttp.ClientSession() as s:
            with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                await s.ws_connect(server.url + "/ws", headers={"Origin": "http://evil.example"})
            assert e.value.status == 403
            ws = await s.ws_connect(server.url + "/ws", headers={"Origin": server.url})  # same origin is fine
            assert json.loads((await ws.receive(timeout=5)).data)["type"] == "hello"
            await ws.close()
    run(go())


def test_benchmark_command_produces_the_three_controller_table(server):
    async def go():
        async with WsClient(server.url) as c:
            await c.idle()
            await c.send("benchmark", scenario="balanced")
            msg = await c.until(lambda m: m["type"] == "benchmarks" and not m["busy"] and "balanced" in m["results"], 60)
            doc = msg["results"]["balanced"]
            assert set(doc["controllers"]) == set(scenarios.CONTROLLERS)
            assert doc["seed"] == scenarios.SEED and doc["scenario"]["ew"] == 300
            f, q = doc["controllers"]["fixed"], doc["controllers"]["queue_based"]
            assert doc["improvement_vs_fixed_pct"]["queue_based"]["avg_wait_s"] == round((f["avg_wait_s"] - q["avg_wait_s"]) / f["avg_wait_s"] * 100, 1)
    run(go())


@pytest.mark.slow
def test_a_live_run_to_the_natural_end_reports_the_same_numbers_as_the_benchmark(server):
    """The whole simulated hour through the live server at 50x. Result must equal the headless benchmark exactly."""
    async def go():
        async with WsClient(server.url) as c:
            rid = await start(c, ("queue_based",), scenario="balanced", speed=50)
            res = await c.until(lambda m: m["type"] == "result" and m["run_id"] == rid, 150)
            await c.until_state("ended", 10)
            return res
    res = run(go())
    assert res["complete"] is True and res["panes"][0]["complete"] is True
    live = res["panes"][0]["metrics"]
    saved = json.load(open(os.path.join(paths.RESULTS_DIR, "benchmark_balanced.json")))["controllers"]["queue_based"]
    assert live == saved
    assert live["vehicles_completed"] == 1200
    assert res["panes"][0]["sim_time"] > 3600


def test_no_processes_remain_after_all_tests(server):
    async def go():
        async with WsClient(server.url) as c:
            await c.idle()
    run(go())
    for _ in range(30):
        if not sumo_processes():
            return
        time.sleep(0.1)
    assert sumo_processes() == []
