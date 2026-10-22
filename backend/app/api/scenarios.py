from flask import request

from app import db
from app.api import api_bp
from app.models import Scenario

MAX_VEHS_PER_HOUR = 1500


def valid_rate(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_VEHS_PER_HOUR


@api_bp.route("/scenarios")
def list_scenarios():
    return [s.to_dict() for s in Scenario.query.order_by(Scenario.id).all()]


@api_bp.route("/scenarios/<int:scenario_id>")
def get_scenario(scenario_id):
    scenario = db.session.get(Scenario, scenario_id)
    if scenario is None:
        return {"error": "scenario not found"}, 404
    return scenario.to_dict()


@api_bp.route("/scenarios", methods=["POST"])
def create_scenario():
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    description = data.get("description")
    ew = data.get("ew_vehs_per_hour")
    ns = data.get("ns_vehs_per_hour")

    errors = []
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        errors.append("name must be a non-empty string of at most 64 characters")
    if description is not None and (not isinstance(description, str) or len(description) > 255):
        errors.append("description must be a string of at most 255 characters")
    if not valid_rate(ew):
        errors.append(f"ew_vehs_per_hour must be an integer from 0 to {MAX_VEHS_PER_HOUR}")
    if not valid_rate(ns):
        errors.append(f"ns_vehs_per_hour must be an integer from 0 to {MAX_VEHS_PER_HOUR}")
    if not errors and ew == 0 and ns == 0:
        errors.append("at least one direction needs traffic")
    if errors:
        return {"errors": errors}, 400

    name = name.strip()
    if Scenario.query.filter_by(name=name).first():
        return {"error": "a scenario with that name already exists"}, 409

    scenario = Scenario(
        name=name,
        description=description,
        ew_vehs_per_hour=ew,
        ns_vehs_per_hour=ns,
    )
    db.session.add(scenario)
    db.session.commit()
    return scenario.to_dict(), 201
