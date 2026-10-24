from flask import Blueprint

api_bp = Blueprint("api", __name__)

from app.api import (  # noqa: E402,F401
    auth, compare, desktop_telemetry, errors, export, health, monitor, monitor_desktop, runs, scenarios, timeseries,
)
