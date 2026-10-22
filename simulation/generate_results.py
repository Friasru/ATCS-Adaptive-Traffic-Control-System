import json
import os

import metrics
import runner

REPO_DIR = os.path.abspath(os.path.join(runner.BASE_DIR, ".."))
RESULTS_JSON = os.path.join(REPO_DIR, "results", "results.json")
README = os.path.join(REPO_DIR, "README.md")
ADAPTIVE = "queue_based"

SCENARIOS = [
    ("uneven", "East-west heavy, north-south light", 500, 200),
    ("rush_hour", "Heavy traffic in both directions", 900, 700),
    ("balanced", "Equal light traffic in both directions", 300, 300),
    ("held_out", "North-south heavy, not used for tuning", 200, 600),
]


def measure(controller, name, ew, ns):
    tripinfo, _ = runner.run(controller, ew=ew, ns=ns, tag=f"final_{controller}_{name}")
    stats = {k: round(v, 2) for k, v in metrics.summarize(tripinfo).items()}
    stats["vehicles_completed"] = int(stats["vehicles_completed"])
    return stats


def build_table(entries):
    lines = [
        "| Scenario (EW / NS veh/h) | Avg wait fixed | Avg wait adaptive | Time loss fixed | Time loss adaptive | Max wait fixed | Max wait adaptive |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        f, a = e["fixed"], e["adaptive"]
        lines.append(
            f"| {e['name']} ({e['ew_vehs_per_hour']} / {e['ns_vehs_per_hour']}) "
            f"| {f['avg_wait_s']:.2f} | {a['avg_wait_s']:.2f} "
            f"| {f['avg_time_loss_s']:.2f} | {a['avg_time_loss_s']:.2f} "
            f"| {f['max_wait_s']:.0f} | {a['max_wait_s']:.0f} |"
        )
    return "\n".join(lines)


def update_readme(table):
    with open(README) as f:
        text = f.read()
    start = text.find("## Results")
    end = text.find("## Structure")
    if start == -1 or end == -1 or end < start:
        print("README sections not found, skipped")
        return
    section = (
        "## Results\n\n"
        "Fixed timer vs. queue-based adaptive controller (minimum green 20 s, switch margin 2 cars). "
        "One simulated hour per scenario, times in seconds. The held-out scenario was not used when tuning.\n\n"
        + table
        + "\n\nThe adaptive controller wins in uneven, balanced and held-out traffic. "
        "In saturated rush-hour traffic the fixed timer has slightly lower average wait and time loss (the adaptive controller has the lower worst-case wait), because both roads are busy "
        "and there is little room to improve. The parameter sweep is in results/queue_based_tuning.txt.\n\n"
    )
    with open(README, "w") as f:
        f.write(text[:start] + section + text[end:])


def main():
    entries = []
    for name, description, ew, ns in SCENARIOS:
        entries.append(
            {
                "name": name,
                "description": description,
                "ew_vehs_per_hour": ew,
                "ns_vehs_per_hour": ns,
                "fixed": measure("fixed", name, ew, ns),
                "adaptive": measure(ADAPTIVE, name, ew, ns),
            }
        )
    with open(RESULTS_JSON, "w") as f:
        json.dump({"adaptive_controller": ADAPTIVE, "scenarios": entries}, f, indent=2)
        f.write("\n")
    table = build_table(entries)
    update_readme(table)
    print(table)


if __name__ == "__main__":
    main()
