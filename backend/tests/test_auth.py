from datetime import timedelta

import pytest
from flask_jwt_extended import create_access_token, decode_token

from app import bcrypt
from app.models import User

USER = {"username": "alberto", "password": "correct-horse"}


def register(client, **fields):
    return client.post("/api/auth/register", json={**USER, **fields})


def login(client, **fields):
    return client.post("/api/auth/login", json={**USER, **fields})


def test_register_success(client):
    response = register(client)
    data = response.get_json()
    assert response.status_code == 201
    assert data["username"] == "alberto"
    assert set(data) == {"id", "username"}


def test_register_duplicate_username(client):
    register(client)
    response = register(client, password="another-password")
    assert response.status_code == 409


def test_password_is_hashed(client, app):
    register(client)
    with app.app_context():
        user = User.query.filter_by(username="alberto").one()
        assert user.password_hash != USER["password"]
        assert USER["password"] not in user.password_hash
        assert user.password_hash.startswith("$2")
        assert bcrypt.check_password_hash(user.password_hash, USER["password"])


@pytest.mark.parametrize(
    "body",
    [
        {"username": "ab", "password": "long-enough"},
        {"username": "x" * 65, "password": "long-enough"},
        {"username": "has space", "password": "long-enough"},
        {"username": "", "password": "long-enough"},
        {"username": 123, "password": "long-enough"},
        {"username": "alberto", "password": "short"},
        {"username": "alberto", "password": "p" * 73},
        {"username": "alberto", "password": 12345678},
        {"username": "alberto"},
        {"password": "long-enough"},
        {},
        ["alberto", "long-enough"],
    ],
)
def test_register_invalid(client, body):
    response = client.post("/api/auth/register", json=body)
    assert response.status_code == 400
    assert response.get_json()["errors"]


def test_register_malformed_json(client):
    response = client.post("/api/auth/register", data="{not json", content_type="application/json")
    assert response.status_code == 400


def test_register_response_never_contains_hash(client):
    text = register(client).get_data(as_text=True)
    assert "password" not in text
    assert "$2" not in text


def test_login_success_issues_jwt(client, app):
    user_id = register(client).get_json()["id"]
    response = login(client)
    data = response.get_json()
    assert response.status_code == 200
    assert data["token_type"] == "Bearer"
    assert data["expires_in"] == 3600
    with app.app_context():
        assert decode_token(data["access_token"])["sub"] == str(user_id)


def test_login_wrong_password(client):
    register(client)
    response = login(client, password="wrong-password")
    assert response.status_code == 401
    assert response.get_json() == {"error": "invalid username or password"}


def test_login_unknown_user_same_response(client):
    register(client)
    wrong_password = login(client, password="wrong-password")
    unknown_user = login(client, username="nobody")
    assert unknown_user.status_code == 401
    assert unknown_user.get_json() == wrong_password.get_json()


@pytest.mark.parametrize(
    "body", [{}, {"username": "alberto"}, {"password": "x"}, {"username": "", "password": ""}]
)
def test_login_missing_credentials(client, body):
    assert client.post("/api/auth/login", json=body).status_code == 400


def test_login_without_body(client):
    assert client.post("/api/auth/login").status_code == 400


PROTECTED = ["/api/compare?scenario_id=1", "/api/runs/export.csv"]


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_requires_jwt(client, path):
    response = client.get(path)
    assert response.status_code == 401
    assert response.get_json() == {"error": "authentication required"}


@pytest.mark.parametrize("path", PROTECTED)
@pytest.mark.parametrize(
    "header", ["Bearer not-a-token", "Bearer a.b.c", "Token abc", "Bearer "]
)
def test_protected_rejects_invalid_jwt(client, path, header):
    response = client.get(path, headers={"Authorization": header})
    assert response.status_code == 401


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_rejects_token_signed_with_other_key(client, app, path):
    original = app.config["JWT_SECRET_KEY"]
    app.config["JWT_SECRET_KEY"] = "a-different-secret-key-also-32-bytes-long"
    with app.app_context():
        forged = create_access_token(identity="1")
    app.config["JWT_SECRET_KEY"] = original
    assert client.get(path, headers={"Authorization": f"Bearer {forged}"}).status_code == 401


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_rejects_expired_jwt(client, app, path):
    with app.app_context():
        token = create_access_token(identity="1", expires_delta=timedelta(seconds=-1))
    response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.get_json() == {"error": "token expired"}


@pytest.mark.parametrize("path", PROTECTED)
def test_protected_accepts_login_token(client, path):
    register(client)
    token = login(client).get_json()["access_token"]
    assert client.get(path, headers={"Authorization": f"Bearer {token}"}).status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/health", "/api/controllers", "/api/scenarios", "/api/scenarios/1", "/api/runs"]
)
def test_existing_get_endpoints_stay_public(client, path):
    assert client.get(path).status_code == 200


def test_existing_post_endpoints_stay_public(client):
    created = client.post(
        "/api/scenarios", json={"name": "public", "ew_vehs_per_hour": 100, "ns_vehs_per_hour": 100}
    )
    assert created.status_code == 201
    # 400 from validation, not 401: no token needed to reach the handler
    assert client.post("/api/runs", json={"scenario_id": 1, "controller": "magic"}).status_code == 400


def test_dev_secret_used_only_as_fallback(monkeypatch):
    import importlib

    import config

    monkeypatch.setenv("JWT_SECRET_KEY", "from-the-environment-0123456789abcdef")
    try:
        assert importlib.reload(config).Config.JWT_SECRET_KEY == "from-the-environment-0123456789abcdef"
        monkeypatch.delenv("JWT_SECRET_KEY")
        assert importlib.reload(config).Config.JWT_SECRET_KEY == config.DEV_JWT_SECRET
    finally:
        importlib.reload(config)
