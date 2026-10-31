"""Independent recording of SUMO state, written *without* the bridge.

Runs the ATCS loop exactly as ATCS runner.py does and, after every simulationStep (before controller.step), reads the
state with plain per-vehicle TraCI getters (the bridge uses TraCI subscriptions). Writes JSON to the path in argv[3].

    python independent_reference.py <controller> <steps> <out.json> <ew> <ns>
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(ROOT), "simulation"))  # the repository's own ATCS controllers
import runner  # noqa: E402  (verbatim ATCS runner module, used for write_routes and its config)
import traci  # noqa: E402
from controllers import CONTROLLERS  # noqa: E402

controller, steps, out, ew, ns = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
os.makedirs(os.path.dirname(out), exist_ok=True)
routes = out + ".rou.xml"
runner.write_routes(routes, ew, ns)
# baseline.sumocfg names its own output files, which would land in simulation/; send them next to the recording instead
traci.start(["sumo", "-c", runner.SUMOCFG, "--route-files", routes, "--tripinfo-output", out + ".tripinfo.xml",
             "--summary-output", out + ".summary.xml", "--seed", "23423", "--no-step-log", "true"])
ctl = CONTROLLERS[controller](runner.TLS_ID)
rec = []
for _ in range(steps):
    traci.simulationStep()
    now = traci.simulation.getTime()
    vehs = {}
    for vid in traci.vehicle.getIDList():
        x, y = traci.vehicle.getPosition(vid)
        vehs[vid] = [x, y, traci.vehicle.getAngle(vid), traci.vehicle.getSpeed(vid), traci.vehicle.getLaneID(vid)]
    rec.append({"t": now, "veh": vehs, "tls": traci.trafficlight.getRedYellowGreenState(runner.TLS_ID)})
    ctl.step(now)
traci.close()
json.dump(rec, open(out, "w"))
