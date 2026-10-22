import argparse
import os

import traci

from controllers import CONTROLLERS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TLS_ID = "A0"
SUMOCFG = os.path.join(BASE_DIR, "baseline.sumocfg")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")


def write_routes(path, ew, ns):
    flows = [
        ("we", "west_east", ew),
        ("ew", "east_west", ew),
        ("ns", "north_south", ns),
        ("sn", "south_north", ns),
    ]
    lines = [
        "<routes>",
        '    <route id="west_east" edges="left0A0 A0right0"/>',
        '    <route id="east_west" edges="right0A0 A0left0"/>',
        '    <route id="north_south" edges="top0A0 A0bottom0"/>',
        '    <route id="south_north" edges="bottom0A0 A0top0"/>',
    ]
    for flow_id, route, rate in flows:
        if rate > 0:
            lines.append(
                f'    <flow id="f_{flow_id}" route="{route}" begin="0" end="3600" vehsPerHour="{rate}"/>'
            )
    lines.append("</routes>")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")


def run(name, gui=False, ew=500, ns=200, tag=None):
    tag = tag or name
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    routes = os.path.join(OUTPUT_DIR, f"routes_{tag}.rou.xml")
    tripinfo = os.path.join(OUTPUT_DIR, f"tripinfo_{tag}.xml")
    summary = os.path.join(OUTPUT_DIR, f"summary_{tag}.xml")
    write_routes(routes, ew, ns)
    cmd = [
        "sumo-gui" if gui else "sumo",
        "-c", SUMOCFG,
        "--route-files", routes,
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
    parser.add_argument("--ew", type=int, default=500)
    parser.add_argument("--ns", type=int, default=200)
    parser.add_argument("--tag")
    args = parser.parse_args()
    run(args.controller, args.gui, args.ew, args.ns, args.tag)
