import itertools
import os

import generate_results
import metrics
import runner
from controllers import wait_time
from tune import SCENARIOS

HELD_OUT = next((name, ew, ns) for name, _, ew, ns in generate_results.SCENARIOS if name == "held_out")
MIN_GREENS = [10, 15, 20, 25, 30]
MARGINS = [0, 25, 50, 100, 200]
MAX_GREENS = [45, 60]
RESULTS_TXT = os.path.join(generate_results.REPO_DIR, "results", "wait_time_tuning.txt")


def run(controller, ew, ns):
    tripinfo, _ = runner.run(controller, ew=ew, ns=ns, tag="tune_wait_time")
    return metrics.summarize(tripinfo)


def configure(min_green, margin, max_green):
    wait_time.MIN_GREEN = min_green
    wait_time.SWITCH_MARGIN = margin
    wait_time.MAX_GREEN = max_green


def evaluate(controller):
    return {name: run(controller, ew, ns) for name, (ew, ns) in SCENARIOS.items()}


def mean_loss(result):
    return sum(result[n]["avg_time_loss_s"] for n in SCENARIOS) / len(SCENARIOS)


def admissible(result, fixed):
    """No training scenario may have a worse worst-case wait than the fixed timer."""
    return all(result[n]["max_wait_s"] <= fixed[n]["max_wait_s"] for n in SCENARIOS)


def select(rows, fixed):
    """Lowest mean time loss among admissible configs, or None if none qualify.

    Exact ties go to the smaller (min_green, margin, max_green) tuple, so equal
    results prefer the tighter max-green cap.
    """
    candidates = [(mean_loss(r), cfg) for cfg, r in rows if admissible(r, fixed)]
    return min(candidates)[1] if candidates else None


def label(cfg):
    return "min_green={} margin={} max_green={}".format(*cfg)


def table(rows, fixed):
    entries = [(mean_loss(fixed), "fixed", fixed, "baseline")]
    entries += [(mean_loss(r), label(cfg), r, "ok" if admissible(r, fixed) else "max_wait") for cfg, r in rows]
    entries.sort(key=lambda e: e[0])
    lines = [
        "mean_loss  " + "  ".join(f"{n:>8}" for n in SCENARIOS)
        + "  " + "  ".join(f"{'mw_' + n:>11}" for n in SCENARIOS) + "  status    config"
    ]
    for mean, name, r, status in entries:
        lines.append(
            f"{mean:9.2f}  "
            + "  ".join(f"{r[n]['avg_time_loss_s']:8.2f}" for n in SCENARIOS)
            + "  " + "  ".join(f"{r[n]['max_wait_s']:11.1f}" for n in SCENARIOS)
            + f"  {status:<8}  {name}"
        )
    return lines


def validation(cfg):
    name, ew, ns = HELD_OUT
    fixed = run("fixed", ew, ns)
    configure(*cfg)
    adaptive = run("wait_time", ew, ns)
    lines = [f"Held-out validation: {name} ({ew} / {ns} veh/h), not used for selection", ""]
    lines.append(f"{'':10}{'time_loss':>10}{'avg_wait':>10}{'max_wait':>10}")
    for who, r in (("fixed", fixed), ("wait_time", adaptive)):
        lines.append(f"{who:10}{r['avg_time_loss_s']:10.2f}{r['avg_wait_s']:10.2f}{r['max_wait_s']:10.1f}")
    loss_ok = adaptive["avg_time_loss_s"] < fixed["avg_time_loss_s"]
    wait_ok = adaptive["max_wait_s"] <= fixed["max_wait_s"]
    lines.append("")
    lines.append(f"time loss below fixed: {'yes' if loss_ok else 'NO'}")
    lines.append(f"max wait within fixed: {'yes' if wait_ok else 'NO'}")
    return lines


def main():
    fixed = evaluate("fixed")
    rows = []
    for cfg in itertools.product(MIN_GREENS, MARGINS, MAX_GREENS):
        configure(*cfg)
        rows.append((cfg, evaluate("wait_time")))
    best = select(rows, fixed)
    lines = [
        "Wait-time controller tuning sweep",
        "Training scenarios (EW / NS veh/h): "
        + ", ".join(f"{n} ({ew} / {ns})" for n, (ew, ns) in SCENARIOS.items()),
        "Objective: mean avg time loss over training scenarios (s).",
        "Constraint: max wait in every training scenario <= fixed timer's (status=max_wait means rejected).",
        "Ties: equal mean loss goes to the smaller (min_green, margin, max_green).",
        "",
    ]
    lines += table(rows, fixed)
    lines.append("")
    if best is None:
        lines.append("Selected: none, no configuration met the max-wait constraint")
    else:
        lines.append(f"Selected: {label(best)}")
        lines.append("")
        lines += validation(best)
    text = "\n".join(lines)
    with open(RESULTS_TXT, "w") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
