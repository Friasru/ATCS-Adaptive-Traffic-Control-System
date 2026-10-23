import os
from datetime import timedelta

basedir = os.path.abspath(os.path.dirname(__file__))

# Local development only. Any shared or production deployment must set
# JWT_SECRET_KEY to a long random value; create_app logs a warning otherwise.
DEV_JWT_SECRET = "atcs-dev-only-secret-do-not-use-in-production"


class Config:
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(basedir, "atcs.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SIMULATION_DIR = os.path.abspath(os.path.join(basedir, "..", "simulation"))
    ALLOWED_CONTROLLERS = ["fixed", "queue_based", "wait_time"]
    RUN_TIMEOUT_S = 300
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY") or DEV_JWT_SECRET
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=1)
