from flask import Flask
from flask_bcrypt import Bcrypt
from flask_jwt_extended import JWTManager
from flask_sqlalchemy import SQLAlchemy

from config import DEV_JWT_SECRET, Config

db = SQLAlchemy()
bcrypt = Bcrypt()
jwt = JWTManager()


def unauthorized(reason):
    return {"error": "authentication required"}, 401


def create_app(config_class=Config):
    app = Flask(__name__, static_folder="../../frontend", static_url_path="")
    app.config.from_object(config_class)
    db.init_app(app)
    bcrypt.init_app(app)
    jwt.init_app(app)
    if app.config["JWT_SECRET_KEY"] == DEV_JWT_SECRET and not app.testing:
        app.logger.warning("JWT_SECRET_KEY is not set; using the development-only secret")

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
