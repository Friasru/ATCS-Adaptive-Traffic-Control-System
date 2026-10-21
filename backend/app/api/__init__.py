from flask import Blueprint

api_bp = Blueprint("api", __name__)

from app.api import health, scenarios  # noqa: E402,F401
