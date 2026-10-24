import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(basedir, "atcs.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SIMULATION_DIR = os.path.abspath(os.path.join(basedir, "..", "simulation"))
    ALLOWED_CONTROLLERS = ["fixed", "queue_based", "wait_time"]
    RUN_TIMEOUT_S = 300
    # Required. create_app refuses to start without a strong value unless
    # ATCS_DEV=1, which generates a random secret for that process only.
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY")
    ATCS_DEV = os.environ.get("ATCS_DEV") == "1"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
    # ATCS Monitor local service (monitor/web_service.py, 127.0.0.1 only). The
    # /api/monitor endpoints answer 503 until the shared secret is set; access is
    # granted per account with monitor_access.py.
    MONITOR_SERVICE_PORT = os.environ.get("ATCS_MONITOR_PORT", "8765")
    MONITOR_SERVICE_SECRET = os.environ.get("ATCS_MONITOR_SERVICE_SECRET")
    MONITOR_SERVICE_TIMEOUT_S = 5.0
