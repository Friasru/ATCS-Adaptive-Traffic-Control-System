import os
import signal
import subprocess
import sys

from flask import current_app

from app import db

# Returned to API clients on failure. Exception text can contain absolute
# paths and the full command line, so details go to the server log only.
SIMULATION_ERROR = "simulation failed"


def run_simulation(cmd, cwd, timeout):
    """Run runner.py in its own process group and stop the whole group on timeout.

    Killing only runner.py can leave the sumo process it launched waiting on
    its TraCI port indefinitely.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass
        exc.stdout, exc.stderr = proc.communicate()
        raise
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)


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

        run_simulation(cmd, sim_dir, current_app.config["RUN_TIMEOUT_S"])
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
