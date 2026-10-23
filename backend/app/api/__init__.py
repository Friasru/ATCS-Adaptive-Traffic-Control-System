from flask import Blueprint

api_bp = Blueprint("api", __name__)

from app.api import auth, compare, export, health, runs, scenarios, timeseries  # noqa: E402,F401
