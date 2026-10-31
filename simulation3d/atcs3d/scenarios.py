"""Traffic scenarios and controller names.

The four scenarios are the ones the ATCS project uses for its published results
(ATCS `simulation/generate_results.py`, SCENARIOS). Demand is expressed exactly as the ATCS runner expects:
vehicles per hour on the east-west and north-south axes. Route files are written by the ATCS
`runner.write_routes` itself, so every controller sees byte-identical demand.
"""
import os
from dataclasses import dataclass

from . import paths

# Fixed seed. The ATCS runner passes no seed, so SUMO uses its built-in default (23423); we pass it explicitly
# so the value is visible, and tests assert both give the same results.
SEED = 23423
STEP_LENGTH_S = 1.0  # SUMO default, which the ATCS controllers were tuned for. Do not change.
SIM_END_S = 3600  # baseline.sumocfg end time / flow end time

CONTROLLERS = {
    "fixed": "Fixed timing",
    "queue_based": "Queue-Based",
    "wait_time": "Wait-Time",
}


@dataclass(frozen=True)
class Scenario:
    key: str
    title: str
    description: str
    ew: int  # vehicles/hour west<->east (each direction)
    ns: int  # vehicles/hour north<->south (each direction)

    def to_dict(self):
        return {"key": self.key, "title": self.title, "description": self.description, "ew": self.ew, "ns": self.ns}


SCENARIOS = {
    s.key: s
    for s in [
        Scenario("uneven", "Uneven traffic", "East-west heavy, north-south light", 500, 200),
        Scenario("balanced", "Balanced traffic", "Equal light traffic in both directions", 300, 300),
        Scenario("held_out", "North-south heavy", "North-south heavy (not used when tuning the controllers)", 200, 600),
        Scenario("rush_hour", "Rush hour", "Heavy traffic in both directions", 900, 700),
    ]
}
DEFAULT_SCENARIO = "uneven"


def write_routes(scenario, tag):
    """Write the demand file with the original ATCS writer. Returns its path."""
    paths.use_atcs_source()
    import runner  # the verbatim ATCS runner

    out_dir = os.path.join(paths.OUTPUT_DIR, tag)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "routes.rou.xml")
    runner.write_routes(path, scenario.ew, scenario.ns)
    return path
