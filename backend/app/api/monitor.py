"""Monitor endpoints: authenticated proxy to the local ATCS Monitor service.

Access is deny-by-default. Only accounts given a grant with backend/monitor_access.py
can use these endpoints: viewers read, operators also start/stop monitoring and
recording. A grant is bound to one account (id, name and a fingerprint of its
salted password hash), so registering a granted name again, for example after the
database was recreated, gets nothing.
"""

import re
from functools import wraps

from flask import Response, current_app, request
from flask_jwt_extended import get_jwt_identity, jwt_required

from app import db
from app.api import api_bp
from app.models import MonitorGrant, User
from app.services.monitor_client import STOP_TIMEOUT_S, MonitorClient, MonitorError

SOURCE_ID_RE = re.compile(r"camera:\d{1,3}|video:[A-Za-z0-9 _.()\-]{1,128}")
MAX_BODY_BYTES = 4096
FORBIDDEN = {"error": "not authorized for the monitor"}


def current_role():
    """'operator', 'viewer' or None for the account in the JWT."""
    identity = get_jwt_identity()
    if not isinstance(identity, str) or not identity.isascii() or not identity.isdigit():
        return None
    user = db.session.get(User, int(identity))
    if user is None:
        return None
    grant = MonitorGrant.query.filter_by(user_id=user.id).first()
    return grant.role if grant is not None and grant.matches(user) else None


def monitor_access(operator=False):
    """jwt_required (401) plus a valid monitor grant (403)."""

    def decorator(fn):
        @wraps(fn)
        @jwt_required()
        def wrapper(*args, **kwargs):
            role = current_role()
            if role != "operator" and (operator or role != "viewer"):
                return FORBIDDEN, 403
            try:
                return fn(*args, **kwargs)
            except MonitorError as exc:
                return exc.to_response()

        return wrapper

    return decorator


def client():
    return MonitorClient.from_config(current_app.config)


def json_body():
    """Request body as a dict; empty body is {}. None if it is not a small JSON object."""
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        return None
    if not request.get_data(cache=True):
        return {}
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def no_params():
    if json_body() != {}:
        raise MonitorError(400, "invalid_body", "this endpoint takes no parameters")


@api_bp.route("/monitor/status")
@monitor_access()
def monitor_status():
    return client().get_json("/status")


@api_bp.route("/monitor/sources")
@monitor_access()
def monitor_sources():
    return client().get_json("/sources")


@api_bp.route("/monitor/sessions")
@monitor_access()
def monitor_sessions():
    return client().get_json("/sessions")


@api_bp.route("/monitor/frame.jpg")
@monitor_access()
def monitor_frame():
    jpeg, index = client().get_frame()
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if index:
        headers["X-Frame-Index"] = index
    return Response(jpeg, mimetype="image/jpeg", headers=headers)


@api_bp.route("/monitor/start", methods=["POST"])
@monitor_access(operator=True)
def monitor_start():
    data = json_body()
    if data is None:
        return {"error": "request body must be a JSON object", "code": "invalid_body"}, 400
    unknown = set(data) - {"source", "loop"}
    if unknown:
        return {"error": "unknown field(s): " + ", ".join(sorted(unknown))[:200], "code": "invalid_body"}, 400
    source = data.get("source")
    if not isinstance(source, str) or not SOURCE_ID_RE.fullmatch(source):
        return {"error": "source must be the id of an allowed source", "code": "invalid_source"}, 400
    loop = data.get("loop", False)
    if not isinstance(loop, bool):
        return {"error": "loop must be true or false", "code": "invalid_body"}, 400
    return client().post_json("/start", {"source": source, "loop": loop}), 202


@api_bp.route("/monitor/stop", methods=["POST"])
@monitor_access(operator=True)
def monitor_stop():
    no_params()
    return client().post_json("/stop", timeout=STOP_TIMEOUT_S)


@api_bp.route("/monitor/recording/start", methods=["POST"])
@monitor_access(operator=True)
def monitor_recording_start():
    no_params()
    return client().post_json("/recording/start"), 202


@api_bp.route("/monitor/recording/stop", methods=["POST"])
@monitor_access(operator=True)
def monitor_recording_stop():
    no_params()
    return client().post_json("/recording/stop"), 202
