"""Filesystem locations.

Adapted for the main ATCS repository. The standalone 3dATCS project kept its own verbatim copy of the ATCS
simulation files in `atcs_source/`; here the controllers, runner, metrics and SUMO network are the repository's own
`simulation/` directory, so there is a single copy of the signal logic and it cannot drift. Nothing in this package
writes into `simulation/`: SUMO output goes to OUTPUT_DIR, and the demand file is written there too.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # simulation3d/
REPO_ROOT = os.path.dirname(ROOT)
ATCS_SOURCE = os.path.join(REPO_ROOT, "simulation")  # the real ATCS controllers, runner, metrics and SUMO network
WEB_DIR = os.path.join(ROOT, "web")
OUTPUT_DIR = os.path.join(ROOT, "output")  # transient SUMO output (git-ignored)
RESULTS_DIR = os.environ.get("ATCS3D_RESULTS_DIR") or os.path.join(ROOT, "results")  # committed snapshot of benchmark results

NET_FILE = os.path.join(ATCS_SOURCE, "intersection_final.net.xml")
SUMOCFG = os.path.join(ATCS_SOURCE, "baseline.sumocfg")
TLS_ID = "A0"  # same junction id the original runner controls


def use_atcs_source():
    """Make the ATCS modules (controllers, runner, metrics) importable."""
    if ATCS_SOURCE not in sys.path:
        sys.path.insert(0, ATCS_SOURCE)
