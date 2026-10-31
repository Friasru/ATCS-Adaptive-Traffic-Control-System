"""Helpers shared by the bridge and server tests."""
import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time

import aiohttp

from atcs3d import paths
from atcs3d.worker_client import WorkerClient


async def collect(controller, ew, ns, steps=None, tag="t", routes=None):
    """Drive one worker second by second (what the live server does) and return every snapshot, t=0 first."""
    w = await WorkerClient(tag).spawn()
    try:
        r = await w.request({"cmd": "start", "controller": controller, "scenario": {"ew": ew, "ns": ns}, "tag": tag, "run_id": 1, "routes": routes})
        frames = [r["frame"]]
        n = 0
        while steps is None or n < steps:
            f = await w.request({"cmd": "step"}, timeout=120)
            frames.append(f)
            n += 1
            if f.get("ended"):
                break
        res = await w.request({"cmd": "stop"})
    finally:
        await w.kill()
    return frames, res


def sumo_processes():
    """PIDs of sumo/sim_worker processes started by this project (identified by their command line)."""
    out = []
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if ("atcs3d.sim_worker" in cmd) or (cmd.startswith("sumo ") and paths.ROOT in cmd):
            out.append((int(pid), cmd[:100]))
    return out


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ServerProc:
    """The real 3dATCS server on a private port."""

    def __init__(self, results_dir=None):
        self.results_dir = results_dir
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        env = dict(os.environ)
        if self.results_dir:
            env["ATCS3D_RESULTS_DIR"] = self.results_dir
        self.proc = subprocess.Popen([sys.executable, "-m", "atcs3d.server", "--port", str(self.port)], cwd=paths.ROOT, env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class WsClient:
    """Minimal protocol client: keeps every message so tests can assert on ordering."""

    def __init__(self, url):
        self.url = url
        self.log = []

    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url + "/ws")
        self.hello = json.loads((await self.ws.receive(timeout=10)).data)
        assert self.hello["type"] == "hello"
        return self

    async def __aexit__(self, *exc):
        await self.ws.close()
        await self.session.close()

    async def send(self, cmd, **kw):
        await self.ws.send_str(json.dumps({"cmd": cmd, **kw}))

    async def recv(self, timeout=10):
        m = await self.ws.receive(timeout=timeout)
        msg = json.loads(m.data)
        self.log.append(msg)
        return msg

    async def until(self, pred, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            msg = await self.recv(timeout=max(0.1, end - time.time()))
            if pred(msg):
                return msg
        raise TimeoutError("condition not met")

    async def until_state(self, status, timeout=30):
        return await self.until(lambda m: m["type"] == "state" and m["status"] == status, timeout)

    async def idle(self):
        """Bring the server to a state where nothing is running."""
        await self.send("stop")
        await asyncio.sleep(0.3)


def assert_counter_invariants(panes):
    """The HUD counters mean what their labels say, in every frame of one pane (a worker frame or a server pane).

    `panes` is that pane's frames in order, the t=0 frame first. One function for every caller, so the single view, both
    panes of a side-by-side run and the worker itself are held to the same definitions:

      active     = vehicles on the road now                = in_network, and spawned - completed
      spawned    = cumulative, moves by exactly the ids that appeared since the previous frame (never decreases)
      completed  = cumulative, moves by exactly the ids that left        (never decreases)
      stopped    = vehicles SUMO is counting as waiting (waiting time running), network-wide and per approach lane
      halt       = SUMO's own lane halting number; it differs from `stopped` only by vehicles placed this second at rest
    """
    prev_ids, prev = set(), None
    for f in panes:
        t, c, vs = f["t"], f["counts"], f["vehicles"]
        ids = {v["id"] for v in vs}
        new, gone = ids - prev_ids, prev_ids - ids
        assert len(ids) == len(vs), f"t={t}: duplicate vehicle ids"
        assert c["in_network"] == len(vs) == c["departed"] - c["arrived"], f"t={t}: active != spawned - completed: {c}"
        if prev is not None:
            pc = prev["counts"]
            assert c["departed"] - pc["departed"] == len(new), f"t={t}: spawned moved by {c['departed'] - pc['departed']}, ids that appeared: {sorted(new)}"
            assert c["arrived"] - pc["arrived"] == len(gone), f"t={t}: completed moved by {c['arrived'] - pc['arrived']}, ids that left: {sorted(gone)}"
        for v in vs:
            if v["id"] in new:
                assert v["v"] == 0 and v["w"] == 0, f"t={t}: {v['id']} appears already moving or already waiting: {v}"
        assert c["stopped"] == sum(v["w"] > 0 for v in vs), f"t={t}: stopped {c['stopped']} != vehicles with SUMO waiting time > 0"
        for lane, d in f["lanes"].items():
            on = [v for v in vs if v["lane"] == lane]
            placed_at_rest = sum(1 for v in on if v["id"] in new and v["v"] < 0.1)
            assert d["stopped"] == sum(v["w"] > 0 for v in on), f"t={t} {lane}: stopped {d['stopped']} != waiting vehicles on the lane"
            assert d["halt"] - d["stopped"] == placed_at_rest, f"t={t} {lane}: halt {d['halt']} - stopped {d['stopped']} != {placed_at_rest} placed at rest"
        assert sum(g["stopped"] for g in f["groups"]) == sum(d["stopped"] for d in f["lanes"].values()), f"t={t}: group totals != lane totals"
        assert sum(g["halt"] for g in f["groups"]) == sum(d["halt"] for d in f["lanes"].values())
        prev_ids, prev = ids, f
