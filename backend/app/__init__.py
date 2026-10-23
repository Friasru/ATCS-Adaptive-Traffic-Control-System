import hashlib
import secrets

from flask import Flask
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager
from flask_sqlalchemy import SQLAlchemy

from config import Config

MIN_JWT_SECRET_BYTES = 32
# SHA-256 of secrets that appear in this repository's history and so can
# never be real ones (the former development fallback).
KNOWN_SECRET_HASHES = {"5d81d4a8a15e75736dc65851285e3004279dd7f2ad8722c16a8296cbff145782"}

db = SQLAlchemy()
bcrypt = Bcrypt()
jwt = JWTManager()


def unauthorized(reason):
    return {"error": "authentication required"}, 401


def configure_jwt_secret(app):
    """Fail closed: a strong JWT_SECRET_KEY, or an explicit ATCS_DEV=1 random one.

    Messages never include the secret itself.
    """
    secret = app.config.get("JWT_SECRET_KEY")
    if secret:
        known = hashlib.sha256(secret.encode()).hexdigest() in KNOWN_SECRET_HASHES
        if len(secret.encode()) < MIN_JWT_SECRET_BYTES or known:
            raise RuntimeError(
                f"JWT_SECRET_KEY is insecure: use a random value of at least {MIN_JWT_SECRET_BYTES} bytes, "
                "for example: python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )
        return
    if app.config.get("ATCS_DEV"):
        app.config["JWT_SECRET_KEY"] = secrets.token_hex(32)
        app.logger.warning(
            "ATCS_DEV=1: using a random JWT secret for this process; tokens stop working on restart"
        )
        return
    raise RuntimeError(
        "JWT_SECRET_KEY is not set. Set it to a random value of at least "
        f"{MIN_JWT_SECRET_BYTES} bytes, or set ATCS_DEV=1 for local development."
    )


def create_app(config_class=Config):
    app = Flask(__name__, static_folder="../../frontend", static_url_path="")
    app.config.from_object(config_class)
    configure_jwt_secret(app)
    db.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)

    # Same 401 body for every token problem, in the API's {"error": ...} shape.
    jwt.unauthorized_loader(unauthorized)
    jwt.invalid_token_loader(unauthorized)
    jwt.expired_token_loader(lambda header, payload: ({"error": "token expired"}, 401))

    from app.api import api_bp

    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        from app import models  # noqa: F401
        from app.services.seed import seed_scenarios

        db.create_all()
        seed_scenarios()

    return app
