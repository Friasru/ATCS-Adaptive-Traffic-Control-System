import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(basedir, "atcs.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SIMULATION_DIR = os.path.abspath(os.path.join(basedir, "..", "simulation"))
    ALLOWED_CONTROLLERS = ["fixed", "queue_based"]
    RUN_TIMEOUT_S = 300
