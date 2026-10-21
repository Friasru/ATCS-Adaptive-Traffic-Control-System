import argparse
import os

import traci

from controllers import CONTROLLERS

TLS_ID = "A0"
SUMOCFG = "baseline.sumocfg"
OUTPUT_DIR = "output"


def run(name, gui=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tripinfo = os.path.join(OUTPUT_DIR, f"tripinfo_{name}.xml")
    summary = os.path.join(OUTPUT_DIR, f"summary_{name}.xml")
    cmd = [
        "sumo-gui" if gui else "sumo",
        "-c", SUMOCFG,
        "--tripinfo-output", tripinfo,
        "--summary-output", summary,
    ]
    if gui:
        cmd += ["--start", "--quit-on-end", "--delay", "50"]
    traci.start(cmd)
    controller = CONTROLLERS[name](TLS_ID)
    while traci.simulation.getMinExpectedNumber() > 0:
        traci.simulationStep()
        controller.step(traci.simulation.getTime())
    traci.close()
    return tripinfo, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("controller", choices=sorted(CONTROLLERS))
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()
    run(args.controller, args.gui)
