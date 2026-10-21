from app.api import api_bp
from app.models import Scenario


@api_bp.route("/scenarios")
def list_scenarios():
    return [s.to_dict() for s in Scenario.query.order_by(Scenario.id).all()]


@api_bp.route("/scenarios/<int:scenario_id>")
def get_scenario(scenario_id):
    scenario = Scenario.query.get(scenario_id)
    if scenario is None:
        return {"error": "scenario not found"}, 404
    return scenario.to_dict()
