from flask import current_app, request
from flask_jwt_extended import jwt_required

from app import db
from app.api import api_bp
from app.api.params import parse_id
from app.models import Run, Scenario

BASELINE = "fixed"
# Lower is better for all of these; vehicles_completed is reported but not compared.
METRICS = ["avg_wait_s", "max_wait_s", "avg_time_loss_s", "avg_trip_duration_s"]


def improvement(baseline, value):
    """Percent reduction versus the baseline, or None when it cannot be computed."""
    if baseline is None or value is None or baseline == 0:
        return None
    # "or 0.0" turns -0.0 into 0.0
    return round((baseline - value) / baseline * 100, 1) or 0.0


def latest_completed(scenario_id):
    """Newest completed run per controller: one LIMIT 1 query per controller."""
    completed = Run.query.filter_by(scenario_id=scenario_id, status="completed")
    names = [name for (name,) in completed.with_entities(Run.controller).distinct()]
    return {
        name: completed.filter_by(controller=name).order_by(Run.created_at.desc(), Run.id.desc()).first()
        for name in names
    }


@api_bp.route("/compare")
@jwt_required()
def compare():
    raw = request.args.get("scenario_id")
    if raw is None:
        return {"error": "scenario_id is required"}, 400
    scenario_id = parse_id(raw)
    if scenario_id is None:
        return {"error": "scenario_id must be a positive integer"}, 400
    scenario = db.session.get(Scenario, scenario_id)
    if scenario is None:
        return {"error": "scenario not found"}, 404

    latest = latest_completed(scenario_id)
    order = current_app.config["ALLOWED_CONTROLLERS"]
    names = sorted(latest, key=lambda n: (order.index(n) if n in order else len(order), n))
    fixed = latest.get(BASELINE)
    controllers = []
    for name in names:
        run = latest[name]
        entry = {"controller": name, "run_id": run.id, "vehicles_completed": run.vehicles_completed}
        entry.update({m: getattr(run, m) for m in METRICS})
        entry["improvement_vs_fixed_pct"] = (
            {m: improvement(getattr(fixed, m), getattr(run, m)) for m in METRICS} if fixed else None
        )
        controllers.append(entry)
    return {"scenario": scenario.to_dict(), "baseline": BASELINE, "controllers": controllers}
