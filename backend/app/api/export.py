import csv
import io

from flask import Response, current_app, request
from flask_jwt_extended import jwt_required

from app.api import api_bp
from app.api.params import parse_id
from app.models import Run

COLUMNS = [
    "id",
    "scenario_id",
    "controller",
    "status",
    "created_at",
    "vehicles_completed",
    "avg_wait_s",
    "max_wait_s",
    "avg_time_loss_s",
    "avg_trip_duration_s",
]


def safe_cell(value):
    # Text starting with these is run as a formula by spreadsheet programs.
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


@api_bp.route("/runs/export.csv")
@jwt_required()
def export_runs():
    query = Run.query
    raw_scenario = request.args.get("scenario_id")
    controller = request.args.get("controller")
    if raw_scenario is not None:
        scenario_id = parse_id(raw_scenario)
        if scenario_id is None:
            return {"error": "scenario_id must be a positive integer"}, 400
        query = query.filter_by(scenario_id=scenario_id)
    if controller is not None:
        if controller not in current_app.config["ALLOWED_CONTROLLERS"]:
            return {"error": "unknown controller"}, 400
        query = query.filter_by(controller=controller)

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    for run in query.order_by(Run.id):
        writer.writerow({k: safe_cell(v) for k, v in run.to_dict().items()})
    return Response(
        out.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="atcs_runs.csv"'},
    )
