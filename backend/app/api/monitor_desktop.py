"""/api/monitor/desktop: read-only views of desktop telemetry for the browser.

Same access rules as the other monitor endpoints (JWT, then a viewer or operator
grant). Nothing here contacts a desktop or the monitor service, and nothing
here changes stored data.
"""

from flask import current_app, request

from app import db
from app.api import api_bp
from app.api.monitor import monitor_access
from app.models import DesktopSession, DesktopStatus, MonitorDevice
from app.models.monitor_device import DEVICE_NAME_RE, iso, utcnow
from app.services import desktop_schema
from app.services.desktop_telemetry import session_view, status_view

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def live_instances(now, timeout_s):
    """device id -> instance id of each active device that is reporting right now."""
    out = {}
    for row in DesktopStatus.query.all():
        if (now - row.received_at).total_seconds() <= timeout_s:
            out[row.device_id] = row.instance_id
    return out


@api_bp.route("/monitor/desktop/status")
@monitor_access()
def monitor_desktop_status():
    now = utcnow()
    timeout = current_app.config["DESKTOP_HEARTBEAT_TIMEOUT_S"]
    devices = MonitorDevice.query.filter(MonitorDevice.revoked_at.is_(None)).order_by(MonitorDevice.name).all()
    rows = {r.device_id: r for r in DesktopStatus.query.all()}
    return {
        "heartbeat_timeout_s": timeout,
        "server_time": iso(now),
        "devices": [status_view(d, rows.get(d.id), now, timeout) for d in devices],
    }


@api_bp.route("/monitor/desktop/sessions")
@monitor_access()
def monitor_desktop_sessions():
    unknown = set(request.args) - {"limit", "device"}
    if unknown:
        return {"error": "unknown parameter(s): " + ", ".join(sorted(unknown))[:200], "code": "invalid_query"}, 400
    text = request.args.get("limit", str(DEFAULT_LIMIT))
    if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= MAX_LIMIT:
        return {"error": f"limit must be 1 to {MAX_LIMIT}", "code": "invalid_query"}, 400
    query = DesktopSession.query
    name = request.args.get("device")
    if name is not None:
        if not DEVICE_NAME_RE.fullmatch(name):
            return {"error": "invalid device name", "code": "invalid_query"}, 400
        query = query.join(MonitorDevice).filter(MonitorDevice.name == name)
    rows = query.order_by(DesktopSession.started_at_utc.desc(), DesktopSession.session_id).limit(int(text)).all()
    live = live_instances(utcnow(), current_app.config["DESKTOP_HEARTBEAT_TIMEOUT_S"])
    return {"sessions": [session_view(s, live) for s in rows]}


@api_bp.route("/monitor/desktop/sessions/<session_id>")
@monitor_access()
def monitor_desktop_session(session_id):
    if not desktop_schema.HEX32_RE.fullmatch(session_id):
        return {"error": "session id must be 32 lowercase hex characters", "code": "invalid_session_id"}, 400
    s = db.session.get(DesktopSession, session_id)
    if s is None:
        return {"error": "session not found", "code": "not_found"}, 404
    live = live_instances(utcnow(), current_app.config["DESKTOP_HEARTBEAT_TIMEOUT_S"])
    return session_view(s, live, detail=True)
