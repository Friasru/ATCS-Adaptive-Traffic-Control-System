"""3D Simulator service: what the 3D Simulator page asks before it shows the simulation.

Public like the page itself. The service only listens on 127.0.0.1, and starting it again while it is starting or
running changes nothing (app/services/simulator3d.py).
"""

from flask import current_app, request

from app.api import api_bp


@api_bp.route("/simulator3d/status")
def simulator3d_status():
    return current_app.extensions["simulator3d"].status()


@api_bp.route("/simulator3d/start", methods=["POST"])
def simulator3d_start():
    body = request.get_json(silent=True)
    retry = isinstance(body, dict) and body.get("retry") is True
    return current_app.extensions["simulator3d"].ensure(retry=retry)
