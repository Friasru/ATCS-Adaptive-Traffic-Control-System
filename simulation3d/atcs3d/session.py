"""Run orchestration and the state bridge between the SUMO workers and the browser.

One `Hub` owns at most one active `Run`. A run has one pane (single view) or two (side-by-side); each pane is its own
SUMO + controller process. All panes of a run are stepped in lockstep, one simulated second per tick, so the
two sides always show the same simulation time.

Guarantees the renderer relies on:
  * every message carries `run_id`; the hub bumps it on every start, so frames of a previous run can never be mistaken
    for the current one (no stale lights, no duplicate vehicles after a restart);
  * a frame is a complete snapshot (all vehicles, full signal state), never a delta;
  * pause simply stops asking the workers to step, so the simulation clock is frozen, not faked;
  * commands are serialised by one lock; a run's driver task is cancelled and its workers killed before a new run starts.
"""
import asyncio
import logging
import time
from collections import deque

from . import benchmark, scenarios
from .worker_client import WorkerClient, WorkerError

log = logging.getLogger("atcs3d.session")

SPEEDS = [1, 2, 5, 10, 20, 50]  # simulated seconds per real second (50x shows a full simulated hour in ~72 s)
DEFAULT_SPEED = 5


class Pane:
    def __init__(self, controller, tag):
        self.controller = controller
        self.tag = tag
        self.worker = None
        self.frame = None  # last snapshot
        self.ended = False
        self.metrics = None
        self.complete = False
        self.groups = []


class Run:
    def __init__(self, run_id, controllers, scenario, speed):
        self.run_id = run_id
        self.scenario = scenario
        self.speed = speed
        self.panes = [Pane(c, f"live_{run_id}_{i}_{c}") for i, c in enumerate(controllers)]
        self.gate = asyncio.Event()  # set = running, clear = paused
        self.task = None
        self.seq = 0
        self.status = "starting"  # starting | running | paused | ended | stopped


class Hub:
    def __init__(self):
        self.clients = set()
        self._locks = {}
        self.lock = asyncio.Lock()
        self.run = None
        self.run_counter = 0
        # what the UI has selected for the next run
        self.sel = {"controllers": ["fixed"], "scenario": scenarios.DEFAULT_SCENARIO, "speed": DEFAULT_SPEED}
        self.status = "idle"  # idle | starting | running | paused | ended | stopped
        self.last_result = None
        self.history = []  # finished live runs, newest last (kept for the comparison table)
        self.bench = benchmark.load_saved()
        self.bench_busy = False
        self.step_ms = deque(maxlen=600)  # worker round-trip per tick
        self.frame_bytes = deque(maxlen=600)
        self.auto_paused = False  # we paused because every browser left (as opposed to the presenter pressing Pause)
        self._away_task = None

    # ------------------------------------------------------------------ outgoing
    def state_msg(self):
        run = self.run
        return {
            "type": "state",
            "status": self.status,
            "run_id": run.run_id if run else None,
            "controllers": self.sel["controllers"],
            "mode": "compare" if len(self.sel["controllers"]) == 2 else "single",
            "scenario": scenarios.SCENARIOS[self.sel["scenario"]].to_dict(),
            "speed": self.sel["speed"],
            "t": max((p.frame["t"] for p in run.panes if p.frame), default=0.0) if run else 0.0,
            "busy_benchmark": self.bench_busy,
        }

    async def _send(self, ws, text):
        async with self._locks[ws]:  # one writer per connection at a time, so ordering is exactly the call order
            await asyncio.wait_for(ws.send_str(text), 2.0)

    async def broadcast(self, msg):
        import json

        text = json.dumps(msg, separators=(",", ":"))
        if msg.get("type") == "frame":
            self.frame_bytes.append(len(text))
        clients = list(self.clients)
        results = await asyncio.gather(*(self._send(ws, text) for ws in clients), return_exceptions=True)
        for ws, r in zip(clients, results):
            if isinstance(r, Exception):
                log.warning("dropping a client that could not take %s message (%s)", msg.get("type"), type(r).__name__)
                self.clients.discard(ws)

    async def announce(self):
        msg = self.state_msg()
        log.info("state -> %s run=%s clients=%d", msg["status"], msg["run_id"], len(self.clients))
        await self.broadcast(msg)

    def frame_msg(self, run):
        return {
            "type": "frame",
            "run_id": run.run_id,
            "seq": run.seq,
            "t": max(p.frame["t"] for p in run.panes),
            "srv_ms": int(time.time() * 1000),
            "panes": [dict(p.frame, controller=p.controller, ended=p.ended) for p in run.panes],
        }

    def snapshot_texts(self):
        import json

        dump = lambda m: json.dumps(m, separators=(",", ":"))  # noqa: E731
        texts = [dump(self.hello_msg()), dump(self.state_msg())]
        if self.run and all(p.frame for p in self.run.panes):
            texts.append(dump(self.frame_msg(self.run)))
        if self.last_result:
            texts.append(dump(self.last_result))
        texts.append(dump(self.bench_msg()))
        return texts

    async def attach(self, ws):
        """Register a browser. It always gets hello first, then the current picture, then every later broadcast in order.

        The snapshot is taken and the client registered with no `await` between them, so no state change can fall in the
        gap; broadcasts that start while the hello is still being written queue behind it on the connection's lock.
        """
        self._locks[ws] = asyncio.Lock()
        async with self._locks[ws]:
            texts = self.snapshot_texts()
            self.clients.add(ws)
            for t in texts:
                await asyncio.wait_for(ws.send_str(t), 5.0)

    def detach(self, ws):
        self.clients.discard(ws)
        self._locks.pop(ws, None)

    def hello_msg(self):
        from . import netmodel

        if not hasattr(self, "_net"):
            self._net = netmodel.load_network()
        return {
            "type": "hello",
            "net": self._net,
            "scenarios": [s.to_dict() for s in scenarios.SCENARIOS.values()],
            "controllers": [{"key": k, "title": v} for k, v in scenarios.CONTROLLERS.items()],
            "speeds": SPEEDS,
            "seed": scenarios.SEED,
            "step_length_s": scenarios.STEP_LENGTH_S,
        }

    def bench_msg(self):
        return {"type": "benchmarks", "busy": self.bench_busy, "results": self.bench, "live": self.history[-12:]}

    # ------------------------------------------------------------------ browsers coming and going
    # A run with nobody watching only burns CPU. When the last browser leaves we pause; a reload (reconnect within
    # seconds) resumes by itself, and a long absence stops the run and frees SUMO.
    AWAY_STOP_S = 120

    async def client_joined(self):
        if self._away_task:
            self._away_task.cancel()
            self._away_task = None
        async with self.lock:
            if self.auto_paused and self.status == "paused":
                await self._resume()
            self.auto_paused = False

    async def client_left(self):
        if not self.clients and self._away_task is None:
            self._away_task = asyncio.create_task(self._nobody_watching())

    async def _nobody_watching(self):
        try:
            async with self.lock:
                if self.status == "running":
                    self.auto_paused = True
                    await self._pause()
            await asyncio.sleep(self.AWAY_STOP_S)
            async with self.lock:
                if not self.clients and self.status in ("running", "paused", "starting"):
                    await self._stop("stopped", quiet=True)
        except asyncio.CancelledError:
            pass
        finally:
            self._away_task = None

    # ------------------------------------------------------------------ commands
    async def handle(self, msg):
        cmd = msg.get("cmd")
        async with self.lock:
            if cmd == "select":
                await self._select(msg)
            elif cmd == "start":
                await self._start()
            elif cmd == "pause":
                self.auto_paused = False
                await self._pause()
            elif cmd == "resume":
                await self._resume()
            elif cmd == "stop":
                await self._stop("stopped")
            elif cmd == "restart":
                await self._stop("stopped", quiet=True)
                await self._start()
            elif cmd == "speed":
                await self._speed(msg)
            elif cmd == "benchmark":
                await self._benchmark(msg)
            else:
                await self.broadcast({"type": "error", "message": f"unknown command {cmd!r}"})

    async def _select(self, msg):
        ctrls = msg.get("controllers") or self.sel["controllers"]
        key = msg.get("scenario") or self.sel["scenario"]
        if not (1 <= len(ctrls) <= 2) or any(c not in scenarios.CONTROLLERS for c in ctrls):
            return await self.broadcast({"type": "error", "message": "invalid controller selection"})
        if key not in scenarios.SCENARIOS:
            return await self.broadcast({"type": "error", "message": "unknown scenario"})
        changed = ctrls != self.sel["controllers"] or key != self.sel["scenario"]
        if changed and self.status in ("running", "paused", "starting"):
            # A different controller/scenario is a different experiment: end the current run explicitly (clients get
            # "stopped" and the partial result) instead of silently switching behaviour mid-run.
            await self._stop("stopped")
        self.sel["controllers"], self.sel["scenario"] = list(ctrls), key
        if changed:
            self.run = None
            self.last_result = None
            self.status = "idle"
        await self.announce()

    async def _speed(self, msg):
        v = msg.get("value")
        if v in SPEEDS:
            self.sel["speed"] = v
            if self.run:
                self.run.speed = v
        await self.announce()

    async def _start(self):
        if self.status in ("running", "paused", "starting"):
            return await self.broadcast({"type": "error", "message": "a run is already active; use Restart or Stop"})
        self.run_counter += 1
        scenario = scenarios.SCENARIOS[self.sel["scenario"]]
        run = Run(self.run_counter, self.sel["controllers"], scenario, self.sel["speed"])
        self.run, self.status, self.last_result = run, "starting", None
        await self.announce()
        try:
            for pane in run.panes:
                pane.worker = await WorkerClient(pane.tag).spawn()
            replies = await asyncio.gather(*(
                p.worker.request({"cmd": "start", "controller": p.controller, "run_id": run.run_id, "tag": p.tag,
                                  "scenario": {"ew": scenario.ew, "ns": scenario.ns}}, timeout=60)
                for p in run.panes))
        except Exception as exc:
            await self._kill_panes(run)
            self.status = "idle"
            self.run = None
            return await self.broadcast({"type": "error", "message": f"could not start SUMO: {exc}"})
        for pane, r in zip(run.panes, replies):
            pane.frame, pane.groups = r["frame"], r["groups"]
        run.status = self.status = "running"
        run.gate.set()
        run.task = asyncio.create_task(self._drive(run))
        await self.announce()
        await self.broadcast(self.frame_msg(run))  # t=0 picture: signals visible before the first step

    async def _pause(self):
        if self.status == "running":
            self.run.gate.clear()
            self.run.status = self.status = "paused"
        await self.announce()

    async def _resume(self):
        if self.status == "paused":
            self.run.status = self.status = "running"
            self.run.gate.set()
        await self.announce()

    async def _stop(self, final, quiet=False):
        run = self.run
        if run is None or self.status not in ("running", "paused", "starting"):
            if not quiet:
                await self.announce()
            return
        if run.task:
            run.task.cancel()
            try:
                await run.task
            except (asyncio.CancelledError, Exception):
                pass
        await self._collect(run, complete=False)
        run.status = self.status = final
        if not quiet:
            await self.announce()

    async def _kill_panes(self, run):
        await asyncio.gather(*(p.worker.kill() for p in run.panes if p.worker), return_exceptions=True)

    async def _collect(self, run, complete):
        """Close SUMO for every pane (flushes tripinfo), keep the metrics of what completed, and report them."""
        async def one(p):
            if p.worker is None:
                return
            res = await p.worker.stop()
            p.metrics = (res or {}).get("metrics")
            p.complete = bool(complete and p.ended)

        await asyncio.gather(*(one(p) for p in run.panes), return_exceptions=True)
        panes = []
        for p in run.panes:
            panes.append({"controller": p.controller, "metrics": p.metrics, "complete": p.complete,
                          "sim_time": p.frame["t"] if p.frame else 0.0})
        result = {"type": "result", "run_id": run.run_id, "complete": complete,
                  "scenario": run.scenario.to_dict(), "seed": scenarios.SEED, "panes": panes}
        self.last_result = result
        for p in panes:
            if p["metrics"] and p["metrics"].get("vehicles_completed"):
                self.history.append({"scenario": run.scenario.key, "controller": p["controller"],
                                     "metrics": p["metrics"], "complete": p["complete"],
                                     "sim_time": p["sim_time"], "run_id": run.run_id})
        await self.broadcast(result)
        await self.broadcast(self.bench_msg())

    # ------------------------------------------------------------------ the pacing loop
    async def _drive(self, run):
        loop = asyncio.get_running_loop()
        due = loop.time()
        try:
            while True:
                await run.gate.wait()
                now = loop.time()
                if now - due > 0.5:  # resumed after a pause, or the machine fell behind: do not burst-catch-up
                    due = now
                if due > now:
                    await asyncio.sleep(due - now)
                    if not run.gate.is_set():
                        due = loop.time()
                        continue
                t0 = time.perf_counter()
                live = [p for p in run.panes if not p.ended]
                frames = await asyncio.gather(*(p.worker.request({"cmd": "step"}) for p in live))
                self.step_ms.append((time.perf_counter() - t0) * 1000)
                for p, f in zip(live, frames):
                    f.pop("type", None)
                    if f.pop("ended", False):
                        p.ended = True
                    p.frame = f
                if run is not self.run:  # superseded while waiting on the workers
                    return
                run.seq += 1
                await self.broadcast(self.frame_msg(run))
                if all(p.ended for p in run.panes):
                    break
                due += 1.0 / run.speed
        except (WorkerError, ConnectionError) as exc:
            if run is self.run:
                await self.broadcast({"type": "error", "message": f"simulation process failed: {exc}"})
                run.status = self.status = "stopped"
                await self._kill_panes(run)
                await self.announce()
            return
        # natural end of the simulated hour: every vehicle has left
        async with self.lock:
            if run is self.run and self.status in ("running", "paused"):
                await self._collect(run, complete=True)
                run.status = self.status = "ended"
                await self.announce()

    # ------------------------------------------------------------------ benchmark
    async def _benchmark(self, msg):
        if self.bench_busy:
            return
        if self.status in ("running", "paused", "starting"):
            return await self.broadcast({"type": "error", "message": "stop the live run before running the benchmark"})
        key = msg.get("scenario") or self.sel["scenario"]
        if key not in scenarios.SCENARIOS:
            return
        self.bench_busy = True
        await self.broadcast(self.bench_msg())
        try:
            self.bench[key] = await benchmark.run_benchmark(key)
        except Exception as exc:
            await self.broadcast({"type": "error", "message": f"benchmark failed: {exc}"})
        finally:
            self.bench_busy = False
            await self.broadcast(self.bench_msg())

    async def shutdown(self):
        run = self.run
        if run and self.status in ("running", "paused", "starting"):
            if run.task:
                run.task.cancel()
            await self._kill_panes(run)

    def perf(self):
        def stat(d):
            d = list(d)
            return {"n": len(d), "avg": round(sum(d) / len(d), 2) if d else None, "max": round(max(d), 2) if d else None}

        return {"step_roundtrip_ms": stat(self.step_ms), "frame_bytes": stat(self.frame_bytes),
                "clients": len(self.clients)}
