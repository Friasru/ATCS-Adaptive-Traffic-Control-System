import math
import os
import xml.etree.ElementTree as ET

MAX_POINTS = 200


def summary_path(sim_dir, run_id):
    # Built from the run id only; never from request input.
    return os.path.join(sim_dir, "output", f"summary_run_{int(run_id)}.xml")


def halting_series(path, max_points=MAX_POINTS):
    """(time, halting) pairs from a SUMO summary file, at most max_points of them.

    Longer runs are cut into equal windows of consecutive steps; each window
    gives its start time and its mean halting count. Picking every n-th step
    instead would alias with the signal cycle. The file is streamed so a long
    simulation does not have to fit in memory.
    """
    points = []
    for _, elem in ET.iterparse(path, events=("end",)):
        if elem.tag == "step":
            points.append((float(elem.get("time")), int(elem.get("halting"))))
        elem.clear()
    if len(points) <= max_points:
        return points
    size = math.ceil(len(points) / max_points)
    windows = [points[i:i + size] for i in range(0, len(points), size)]
    return [(w[0][0], round(sum(h for _, h in w) / len(w), 2)) for w in windows]
