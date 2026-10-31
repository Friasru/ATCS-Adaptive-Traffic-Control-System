"""Headless full-hour runs of the three controllers on one scenario: the numbers behind the comparison table.

Each controller is a separate worker process running the unmodified ATCS loop at full speed (about 2 s per
simulated hour). Same network, same demand file contents, same seed, so differences are down to the controller.

    python -m atcs3d.benchmark              # the scenario selected by default
    python -m atcs3d.benchmark --all        # all scenarios; writes results/benchmark_<scenario>.json
"""
import argparse
import asyncio
import datetime
import json
import os
import subprocess

from . import paths, scenarios
from .worker_client import WorkerClient

ATCS_SOURCE_COMMIT = "83f909d00ffc22d6089849d23f3a96479f8fbeab"  # see atcs_source/PROVENANCE.md
METRIC_KEYS = ("avg_wait_s", "max_wait_s", "avg_time_loss_s", "avg_trip_duration_s")


def sumo_version():
    try:
        out = subprocess.run(["sumo", "--version"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()[0].strip()
    except Exception:
        return "unknown"


def improvement_vs_fixed(results):
    """(fixed - controller) / fixed * 100, rounded to 0.1: the same definition the ATCS /api/compare endpoint uses."""
    fixed = results.get("fixed")
    out = {}
    for name, m in results.items():
        if name == "fixed" or not fixed:
            continue
        out[name] = {}
        for k in METRIC_KEYS:
            base = fixed.get(k)
            out[name][k] = None if not base else round((base - m[k]) / base * 100, 1)
    return out


async def _one(controller, scenario, tag):
    w = await WorkerClient(tag).spawn()
    try:
        await w.request({"cmd": "start", "controller": controller, "scenario": {"ew": scenario.ew, "ns": scenario.ns},
                         "tag": tag, "run_id": 0})
        res = await w.request({"cmd": "finish"}, timeout=600)
        return res["metrics"]
    finally:
        await w.kill()


async def run_benchmark(scenario_key, save=True):
    scenario = scenarios.SCENARIOS[scenario_key]
    names = list(scenarios.CONTROLLERS)
    metrics = await asyncio.gather(*(_one(c, scenario, f"bench_{scenario_key}_{c}") for c in names))
    results = dict(zip(names, metrics))
    doc = {
        "kind": "benchmark",
        "description": "Full simulated hour per controller, headless SUMO, unmodified ATCS controllers.",
        "scenario": scenario.to_dict(),
        "seed": scenarios.SEED,
        "step_length_s": scenarios.STEP_LENGTH_S,
        "sim_end_s": scenarios.SIM_END_S,
        "sumo": sumo_version(),
        "atcs_source_commit": ATCS_SOURCE_COMMIT,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "controllers": results,
        "improvement_vs_fixed_pct": improvement_vs_fixed(results),
    }
    if save:
        os.makedirs(paths.RESULTS_DIR, exist_ok=True)
        with open(os.path.join(paths.RESULTS_DIR, f"benchmark_{scenario_key}.json"), "w") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
    return doc


def load_saved():
    """Benchmark documents already stored in results/ (keyed by scenario)."""
    out = {}
    if os.path.isdir(paths.RESULTS_DIR):
        for name in os.listdir(paths.RESULTS_DIR):
            if name.startswith("benchmark_") and name.endswith(".json"):
                with open(os.path.join(paths.RESULTS_DIR, name)) as f:
                    doc = json.load(f)
                out[doc["scenario"]["key"]] = doc
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="run every scenario")
    ap.add_argument("--scenario", default=scenarios.DEFAULT_SCENARIO, choices=sorted(scenarios.SCENARIOS))
    args = ap.parse_args()
    keys = list(scenarios.SCENARIOS) if args.all else [args.scenario]
    for key in keys:
        doc = asyncio.run(run_benchmark(key))
        sc = doc["scenario"]
        print(f"\n{sc['key']}  (EW {sc['ew']} / NS {sc['ns']} veh/h)   seed {doc['seed']}   {doc['sumo']}")
        print(f"{'controller':<12}{'done':>6}{'avg wait':>10}{'max wait':>10}{'time loss':>11}{'trip dur':>10}")
        for c, m in doc["controllers"].items():
            print(f"{c:<12}{m['vehicles_completed']:>6}{m['avg_wait_s']:>10.2f}{m['max_wait_s']:>10.0f}"
                  f"{m['avg_time_loss_s']:>11.2f}{m['avg_trip_duration_s']:>10.2f}")
        for c, imp in doc["improvement_vs_fixed_pct"].items():
            print(f"  {c} vs fixed: avg wait {imp['avg_wait_s']}%  max wait {imp['max_wait_s']}%  time loss {imp['avg_time_loss_s']}%")


if __name__ == "__main__":
    main()
