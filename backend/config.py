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
