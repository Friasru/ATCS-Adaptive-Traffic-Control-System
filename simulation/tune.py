import itertools
import os

import metrics
import runner
from controllers import queue_based

SCENARIOS = {"uneven": (500, 200), "rush": (900, 700), "balanced": (300, 300)}
MIN_GREENS = [10, 15, 20, 25, 30]
MARGINS = [0, 2, 3, 5]


def evaluate(controller):
    result = {}
    for name, (ew, ns) in SCENARIOS.items():
        tripinfo, _ = runner.run(controller, ew=ew, ns=ns, tag="tune")
        result[name] = metrics.summarize(tripinfo)
    return result


def row(label, result):
    losses = [result[n]["avg_time_loss_s"] for n in SCENARIOS]
    max_wait = max(result[n]["max_wait_s"] for n in SCENARIOS)
    return sum(losses) / len(losses), label, losses, max_wait


def main():
    rows = [row("fixed", evaluate("fixed"))]
    for min_green, margin in itertools.product(MIN_GREENS, MARGINS):
        queue_based.MIN_GREEN = min_green
        queue_based.SWITCH_MARGIN = margin
        rows.append(row(f"min_green={min_green} margin={margin}", evaluate("queue_based")))
    rows.sort(key=lambda r: r[0])
    lines = ["mean_loss  " + "  ".join(f"{n:>8}" for n in SCENARIOS) + "  max_wait  config"]
    for mean, label, losses, max_wait in rows:
        lines.append(
            f"{mean:9.2f}  " + "  ".join(f"{v:8.2f}" for v in losses) + f"  {max_wait:8.1f}  {label}"
        )
    text = "\n".join(lines)
    with open(os.path.join(runner.OUTPUT_DIR, "tune_results.txt"), "w") as f:
        f.write(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
