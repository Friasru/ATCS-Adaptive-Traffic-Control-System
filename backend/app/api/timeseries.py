import xml.etree.ElementTree as ET

from flask import current_app

from app import db
from app.api import api_bp
from app.models import Run
from app.services.timeseries import halting_series, summary_path

UNAVAILABLE = {"error": "timeseries not available"}


@api_bp.route("/runs/<int:run_id>/timeseries")
def run_timeseries(run_id):
    run = db.session.get(Run, run_id)
    if run is None:
        return {"error": "run not found"}, 404
    # Only completed runs: an older file with the same run id could be left over.
    if run.status != "completed":
        return UNAVAILABLE, 404
    try:
        points = halting_series(summary_path(current_app.config["SIMULATION_DIR"], run.id))
    except FileNotFoundError:
        return UNAVAILABLE, 404
    except (ET.ParseError, TypeError, ValueError):
        current_app.logger.exception("could not parse summary for run %s", run.id)
        return {"error": "could not read timeseries"}, 500
    return {
        "run_id": run.id,
        "controller": run.controller,
        "time_s": [t for t, _ in points],
        "halting": [h for _, h in points],
    }
