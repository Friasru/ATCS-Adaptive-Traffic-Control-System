import os
import subprocess
import sys

from flask import current_app

from app import db

# Returned to API clients on failure. Exception text can contain absolute
# paths and the full command line, so details go to the server log only.
SIMULATION_ERROR = "simulation failed"


def execute_run(run):
    sim_dir = current_app.config["SIMULATION_DIR"]
    if sim_dir not in sys.path:
        sys.path.insert(0, sim_dir)

    tag = f"run_{run.id}"
    tripinfo = os.path.join(sim_dir, "output", f"tripinfo_{tag}.xml")
    cmd = [
        sys.executable,
        os.path.join(sim_dir, "runner.py"),
        run.controller,
        "--ew", str(run.scenario.ew_vehs_per_hour),
        "--ns", str(run.scenario.ns_vehs_per_hour),
        "--tag", tag,
    ]
    run.status = "running"
    db.session.commit()
    try:
        import metrics

        subprocess.run(
            cmd,
            cwd=sim_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=current_app.config["RUN_TIMEOUT_S"],
        )
        stats = metrics.summarize(tripinfo)
    except Exception as exc:
        current_app.logger.exception(
            "simulation run %s failed (stderr: %s)", run.id, getattr(exc, "stderr", None)
        )
        run.status = "failed"
        db.session.commit()
        return SIMULATION_ERROR
    run.vehicles_completed = int(stats["vehicles_completed"])
    run.avg_wait_s = stats["avg_wait_s"]
    run.max_wait_s = stats["max_wait_s"]
    run.avg_time_loss_s = stats["avg_time_loss_s"]
    run.avg_trip_duration_s = stats["avg_trip_duration_s"]
    run.status = "completed"
    db.session.commit()
    return None
