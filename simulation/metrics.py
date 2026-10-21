import sys
import xml.etree.ElementTree as ET


def summarize(path):
    trips = ET.parse(path).getroot().findall("tripinfo")
    waits = [float(t.get("waitingTime")) for t in trips]
    losses = [float(t.get("timeLoss")) for t in trips]
    durations = [float(t.get("duration")) for t in trips]
    n = len(trips)
    return {
        "vehicles_completed": n,
        "avg_wait_s": sum(waits) / n,
        "max_wait_s": max(waits),
        "avg_time_loss_s": sum(losses) / n,
        "avg_trip_duration_s": sum(durations) / n,
    }


if __name__ == "__main__":
    for key, value in summarize(sys.argv[1]).items():
        print(f"{key}: {value:.2f}")
