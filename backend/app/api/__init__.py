from flask import Blueprint

api_bp = Blueprint("api", __name__)

from app.api import health, runs, scenarios  # noqa: E402,F401
