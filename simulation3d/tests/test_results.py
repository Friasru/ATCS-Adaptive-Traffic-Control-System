"""The committed benchmark numbers reproduce the figures the ATCS project itself published."""
import json
import os

import pytest

from atcs3d import paths

ORIGINAL = paths.REPO_ROOT  # the ATCS results this repository publishes
pytestmark = pytest.mark.skipif(not os.path.isdir(os.path.join(ORIGINAL, "results")), reason="ATCS results/ not found")


def mine(key):
    return json.load(open(os.path.join(paths.RESULTS_DIR, f"benchmark_{key}.json")))


def test_fixed_and_queue_based_equal_the_published_results_json_for_every_scenario():
    published = json.load(open(os.path.join(ORIGINAL, "results", "results.json")))
    assert published["adaptive_controller"] == "queue_based"
    for entry in published["scenarios"]:
        doc = mine(entry["name"])
        assert (doc["scenario"]["ew"], doc["scenario"]["ns"]) == (entry["ew_vehs_per_hour"], entry["ns_vehs_per_hour"])
        for ours, theirs in ((doc["controllers"]["fixed"], entry["fixed"]), (doc["controllers"]["queue_based"], entry["adaptive"])):
            assert ours["vehicles_completed"] == theirs["vehicles_completed"]
            for k in ("avg_wait_s", "max_wait_s", "avg_time_loss_s", "avg_trip_duration_s"):
                assert round(ours[k], 2) == theirs[k], (entry["name"], k)


def test_wait_time_equals_the_figures_in_the_atcs_readme_and_tuning_sweep():
    held_out = mine("held_out")["controllers"]["wait_time"]
    assert round(held_out["avg_time_loss_s"], 2) == 16.27 and held_out["max_wait_s"] == 25  # README table
    sweep = open(os.path.join(ORIGINAL, "results", "wait_time_tuning.txt")).read()
    # best row of the sweep: min_green=20 margin=0 max_green=45 -> uneven 15.35, rush 38.56, balanced 13.10, mean 22.34
    row = next(l for l in sweep.splitlines() if "min_green=20 margin=0 max_green=45" in l)
    uneven, rush, balanced = (round(mine(k)["controllers"]["wait_time"]["avg_time_loss_s"], 2) for k in ("uneven", "rush_hour", "balanced"))
    assert (uneven, rush, balanced) == (15.35, 38.56, 13.10)
    assert all(f"{v:.2f}" in row for v in (uneven, rush, balanced))
    assert round((uneven + rush + balanced) / 3, 2) == 22.34 and "22.34" in row
