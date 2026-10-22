from flask import current_app, request

from app import db
from app.api import api_bp
from app.models import Run, Scenario
from app.services.simulation import execute_run


@api_bp.route("/controllers")
def list_controllers():
    return {"controllers": current_app.config["ALLOWED_CONTROLLERS"]}


@api_bp.route("/runs", methods=["POST"])
def create_run():
    data = request.get_json(silent=True) or {}
    scenario_id = data.get("scenario_id")
    controller = data.get("controller")
    scenario = db.session.get(Scenario, scenario_id) if isinstance(scenario_id, int) else None
    if scenario is None:
        return {"error": "scenario not found"}, 404
    allowed = current_app.config["ALLOWED_CONTROLLERS"]
    if controller not in allowed:
        return {"error": "unknown controller", "allowed": allowed}, 400
    run = Run(scenario_id=scenario.id, controller=controller)
    db.session.add(run)
    db.session.commit()
    error = execute_run(run)
    if error:
        return {"error": error, "run": run.to_dict()}, 500
    return run.to_dict(), 201


@api_bp.route("/runs")
def list_runs():
    query = Run.query
    scenario_id = request.args.get("scenario_id", type=int)
    controller = request.args.get("controller")
    if scenario_id is not None:
        query = query.filter_by(scenario_id=scenario_id)
    if controller:
        query = query.filter_by(controller=controller)
    return [r.to_dict() for r in query.order_by(Run.id).all()]


@api_bp.route("/runs/<int:run_id>")
def get_run(run_id):
    run = db.session.get(Run, run_id)
    if run is None:
        return {"error": "run not found"}, 404
    return run.to_dict()
