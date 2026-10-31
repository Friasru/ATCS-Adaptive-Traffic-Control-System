"""One SUMO simulation + one verbatim ATCS controller, in its own process.

Run as `python -m atcs3d.sim_worker`. The parent talks to it with JSON lines on stdin/stdout:

    -> {"cmd": "start", "controller": "fixed", "scenario": {"ew": 500, "ns": 200}, "tag": "...", "run_id": 3}
    <- {"type": "started", "frame": {...t=0 snapshot...}, "sumo_pid": 123}
    -> {"cmd": "step"}                      one simulated second
    <- {"type": "frame", ...}               snapshot of that second (+ "ended": true on the last one)
    -> {"cmd": "finish"}                    run to the end without snapshots (benchmark)
    <- {"type": "result", "metrics": {...}}
    -> {"cmd": "stop"}                      close SUMO (flushes tripinfo) and exit
    <- {"type": "result", "metrics": {...}}

The run loop is the ATCS runner's loop, in the same order:

    traci.simulationStep(); controller.step(traci.simulation.getTime())

The only addition is a read-only snapshot taken between the two calls. Nothing here decides signal timing:
the controller object is imported unmodified from `atcs_source/controllers`.

The snapshot reads the signal state *before* controller.step(): SUMO applies a signal switch at the start of a
step, so that state is the one that governed the vehicle movement just reported. Reading it after the controller
call would show an adaptive switch one second early compared to the fixed program.
"""
import json
import os
import signal
import sys

from . import paths, scenarios

HALT_SPEED = 0.1  # SUMO counts a vehicle as waiting below this speed (m/s)


def _r(v, n=2):
    return round(float(v), n)


class SimRun:
    def __init__(self, controller, scenario, tag, run_id=0, seed=scenarios.SEED, routes=None):
        paths.use_atcs_source()
        self.controller_name = controller
        self.scenario = scenario
        self.tag = tag
        self.run_id = run_id
        self.seed = seed
        self.routes = routes  # diagnostics only: a custom SUMO route file instead of the ATCS demand
        self.closed = False
        self.ended = False
        self.out_dir = os.path.join(paths.OUTPUT_DIR, tag)
        self.tripinfo = os.path.join(self.out_dir, "tripinfo.xml")
        self.summary = os.path.join(self.out_dir, "summary.xml")
        self.departed_total = 0
        self.arrived_total = 0
        self.wait_acc = {}  # per-vehicle waiting seconds from observed speeds: live estimate only (matches tripinfo for 98% of vehicles)
        self.done_waits = []
        self.phase_start = 0.0
        self.last_phase = None

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        import traci
        from traci import constants as tc

        from controllers import CONTROLLERS

        self.traci = traci
        self.tc = tc
        routes = self.routes or scenarios.write_routes(self.scenario, self.tag)
        # Same command line as ATCS runner.py, plus an explicit seed (equal to SUMO's default) and quiet step log.
        cmd = [
            "sumo", "-c", paths.SUMOCFG,
            "--route-files", routes,
            "--tripinfo-output", self.tripinfo,
            "--summary-output", self.summary,
            "--seed", str(self.seed),
            "--no-step-log", "true",
        ]
        traci.start(cmd)
        self.sumo_pid = getattr(getattr(traci.getConnection(), "_process", None), "pid", None)
        self.controller = CONTROLLERS[self.controller_name](paths.TLS_ID)

        tls = paths.TLS_ID
        self.controlled = list(traci.trafficlight.getControlledLanes(tls))
        self.in_lanes = list(dict.fromkeys(self.controlled))  # one entry per approach lane, in link order
        logic = traci.trafficlight.getAllProgramLogics(tls)[0]
        self.phase_states = [p.state for p in logic.phases]
        # Which approach lanes each green phase serves (same derivation as the controllers use).
        self.groups = []
        for i, state in enumerate(self.phase_states):
            if "G" in state or "g" in state:
                lanes = list(dict.fromkeys(self.controlled[j] for j, c in enumerate(state) if c in "Gg"))
                self.groups.append({"phase": i, "lanes": lanes})
        self.veh_vars = [
            tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_LANE_ID,
            tc.VAR_WAITING_TIME, tc.VAR_ROUTE_ID, tc.VAR_LENGTH, tc.VAR_WIDTH,
        ]
        return self.snapshot()

    def step(self):
        """One simulated second, exactly as the ATCS runner does it, plus a read-only snapshot."""
        traci = self.traci
        traci.simulationStep()
        now = traci.simulation.getTime()
        frame = self.snapshot()  # read-only, before the controller acts (see module docstring)
        self.controller.step(now)
        if traci.simulation.getMinExpectedNumber() <= 0:
            self.ended = True
            frame["ended"] = True
        return frame

    def finish(self):
        """Run to the end with no snapshots: the unmodified ATCS loop."""
        traci = self.traci
        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            self.controller.step(traci.simulation.getTime())
        self.ended = True

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.traci.close()
        except Exception:
            pass

    def metrics(self):
        """Final numbers, from the ATCS `metrics.summarize` on SUMO's own tripinfo file (call after close)."""
        import metrics

        try:
            m = metrics.summarize(self.tripinfo)
        except (ZeroDivisionError, FileNotFoundError, ValueError):
            return {"vehicles_completed": 0}
        m["vehicles_completed"] = int(m["vehicles_completed"])
        return m

    # ------------------------------------------------------------------ snapshot (read-only TraCI getters)
    def snapshot(self):
        """One frame. What each number means (the HUD labels follow these definitions):

        counts.departed   cumulative: vehicles inserted into the network so far. Never decreases.
        counts.arrived    cumulative: vehicles that finished their trip. Never decreases.
        counts.in_network instantaneous: vehicles in the network now (= departed - arrived).
        counts.stopped    instantaneous: vehicles below HALT_SPEED (SUMO's halting definition) now, network-wide.
        lanes[l].stopped  the same, restricted to approach lane l (that direction's queue).
        lanes[l].halt     SUMO's own lane.getLastStepHaltingNumber: what the Queue-Based controller reads.
        lanes[l].wait     SUMO's lane.getWaitingTime: what the Wait-Time controller reads (summed seconds).

        `stopped` equals `halt` except for one case. SUMO inserts a vehicle with speed 0 (no departSpeed in the ATCS
        demand), so by SUMO's definition (speed < 0.1 m/s) it is "halting" during the second it is placed: the lane
        getter and SUMO's own summary-output `halting` both count it (checked in docs/COUNTER_INVESTIGATION.md). That is
        correct SUMO arithmetic but not a stop: the vehicle has not driven yet, sits at the far end of its approach,
        and its waiting time is 0. For the HUD such vehicles are left out of `stopped` for that one second; every
        later second counts them like any other. `stopped` is therefore the number of vehicles whose SUMO waiting time
        is running. `halt` is deliberately left untouched so the controllers' inputs stay visible exactly as they are.
        """
        traci, tc = self.traci, self.tc
        now = traci.simulation.getTime()
        departed = traci.simulation.getDepartedIDList()
        for vid in departed:
            traci.vehicle.subscribe(vid, self.veh_vars)
        self.departed_total += len(departed)
        inserted_now = set(departed)
        res = traci.vehicle.getAllSubscriptionResults()

        vehicles = []
        stopped_total = 0
        placed_at_rest = {}  # lane -> vehicles standing still only because they were inserted this second
        for vid, d in res.items():
            x, y = d[tc.VAR_POSITION]  # front bumper, SUMO metres
            speed = d[tc.VAR_SPEED]
            if speed < HALT_SPEED:  # raw speed, not the rounded one sent to the browser
                if vid in inserted_now:
                    placed_at_rest[d[tc.VAR_LANE_ID]] = placed_at_rest.get(d[tc.VAR_LANE_ID], 0) + 1
                else:
                    stopped_total += 1
            if vid not in self.wait_acc:
                self.wait_acc[vid] = 0.0  # insertion step: speed 0 only because it was just placed; SUMO does not count it
            elif speed < HALT_SPEED:
                self.wait_acc[vid] += scenarios.STEP_LENGTH_S
            vehicles.append(
                {
                    "id": vid,
                    "x": _r(x), "y": _r(y),
                    "a": _r(d[tc.VAR_ANGLE], 1),  # degrees clockwise from north
                    "v": _r(speed),
                    "lane": d[tc.VAR_LANE_ID],
                    "w": _r(d[tc.VAR_WAITING_TIME], 1),
                    "route": d[tc.VAR_ROUTE_ID],
                    "len": _r(d[tc.VAR_LENGTH]), "wid": _r(d[tc.VAR_WIDTH]),
                }
            )
        for vid in traci.simulation.getArrivedIDList():
            self.arrived_total += 1
            self.done_waits.append(self.wait_acc.pop(vid, 0.0))

        tls = paths.TLS_ID
        phase = traci.trafficlight.getPhase(tls)
        if phase != self.last_phase:
            self.last_phase = phase
            self.phase_start = now
        lanes = {}
        for lane in self.in_lanes:
            halt = traci.lane.getLastStepHaltingNumber(lane)  # input of Queue-Based
            lanes[lane] = {
                "halt": halt,
                "stopped": max(0, halt - placed_at_rest.get(lane, 0)),  # what the HUD shows: halt without the insertion second
                "wait": _r(traci.lane.getWaitingTime(lane), 1),  # input of Wait-Time
                "count": traci.lane.getLastStepVehicleNumber(lane),
            }
        groups = []
        for g in self.groups:
            groups.append(
                {
                    "phase": g["phase"],
                    "halt": sum(lanes[l]["halt"] for l in g["lanes"]),
                    "stopped": sum(lanes[l]["stopped"] for l in g["lanes"]),
                    "wait": _r(sum(lanes[l]["wait"] for l in g["lanes"]), 1),
                }
            )
        n_done = len(self.done_waits)
        return {
            "t": _r(now, 1),
            "vehicles": vehicles,
            "tls": {
                "state": traci.trafficlight.getRedYellowGreenState(tls),
                "phase": phase,
                "elapsed": _r(now - self.phase_start, 1),
            },
            "lanes": lanes,
            "groups": groups,
            "counts": {
                "departed": self.departed_total,
                "arrived": self.arrived_total,
                "in_network": len(vehicles),
                "stopped": stopped_total,
            },
            "live": {  # running estimate; the authoritative figures come from tripinfo when the run ends
                "avg_wait": _r(sum(self.done_waits) / n_done, 2) if n_done else None,
                "max_wait": _r(max(self.done_waits), 1) if n_done else None,
            },
        }


# ---------------------------------------------------------------------- JSON-lines protocol
def serve():
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # stray prints (traci "Retrying...", SUMO output) go to stderr, never into the protocol

    def send(obj):
        proto.write(json.dumps(obj, separators=(",", ":")) + "\n")
        proto.flush()

    run = None

    def on_term(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    try:
        for line in sys.stdin:  # EOF (parent died) ends the loop and falls through to cleanup
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            cmd = msg["cmd"]
            try:
                if cmd == "start":
                    sc = scenarios.Scenario("custom", "", "", int(msg["scenario"]["ew"]), int(msg["scenario"]["ns"]))
                    run = SimRun(msg["controller"], sc, msg["tag"], msg.get("run_id", 0), msg.get("seed", scenarios.SEED), msg.get("routes"))
                    frame = run.start()
                    send({"type": "started", "run_id": run.run_id, "sumo_pid": run.sumo_pid, "frame": frame,
                          "groups": run.groups})
                elif cmd == "step":
                    frame = run.step()
                    frame["type"] = "frame"
                    send(frame)
                elif cmd == "finish":
                    run.finish()
                    run.close()
                    send({"type": "result", "run_id": run.run_id, "metrics": run.metrics(), "ended": True})
                elif cmd == "stop":
                    if run:
                        run.close()
                        send({"type": "result", "run_id": run.run_id, "metrics": run.metrics(), "ended": run.ended})
                    else:
                        send({"type": "result", "metrics": {"vehicles_completed": 0}, "ended": False})
                    break
                else:
                    send({"type": "error", "message": f"unknown command {cmd!r}"})
            except SystemExit:
                raise
            except Exception as exc:  # report, then let the parent decide
                send({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
                if run is not None:
                    run.close()
                break
    finally:
        if run is not None:
            run.close()


if __name__ == "__main__":
    serve()
