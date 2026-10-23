import pytest
from flask_jwt_extended import create_access_token

from app import create_app, db
from config import Config


class ConfigForTesting(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    JWT_SECRET_KEY = "test-only-secret-key-at-least-32-bytes-long"
    BCRYPT_LOG_ROUNDS = 4


@pytest.fixture
def app():
    app = create_app(ConfigForTesting)
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth_headers(app):
    with app.app_context():
        token = create_access_token(identity="1")
    return {"Authorization": f"Bearer {token}"}
