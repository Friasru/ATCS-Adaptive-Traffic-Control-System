import importlib
import logging

import pytest
from flask_jwt_extended import create_access_token

from app import MIN_JWT_SECRET_BYTES, create_app, db
from config import Config

STRONG = "s" * MIN_JWT_SECRET_BYTES
FORMER_DEV_FALLBACK = "atcs-dev-only-secret-do-not-use-in-production"


def make_config(secret=None, dev=False):
    class SecretConfig(Config):
        TESTING = True
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        BCRYPT_LOG_ROUNDS = 4
        JWT_SECRET_KEY = secret
        ATCS_DEV = dev

    return SecretConfig


@pytest.fixture
def build():
    apps = []

    def _build(secret=None, dev=False):
        app = create_app(make_config(secret, dev))
        apps.append(app)
        return app

    yield _build
    for app in apps:
        with app.app_context():
            db.session.remove()
            db.drop_all()


@pytest.mark.parametrize("secret", [None, ""])
def test_missing_secret_refuses_to_start(secret):
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY is not set"):
        create_app(make_config(secret))


@pytest.mark.parametrize(
    "secret", ["short", "x" * (MIN_JWT_SECRET_BYTES - 1), FORMER_DEV_FALLBACK]
)
def test_insecure_secret_refuses_to_start(secret):
    with pytest.raises(RuntimeError, match="insecure") as info:
        create_app(make_config(secret))
    assert secret not in str(info.value)


def test_former_fallback_rejected_even_in_dev_mode():
    with pytest.raises(RuntimeError, match="insecure"):
        create_app(make_config(FORMER_DEV_FALLBACK, dev=True))


def test_weak_explicit_secret_not_rescued_by_dev_mode():
    with pytest.raises(RuntimeError, match="insecure"):
        create_app(make_config("short", dev=True))


def test_strong_secret_is_used_unchanged(build):
    app = build(STRONG)
    assert app.config["JWT_SECRET_KEY"] == STRONG


def test_multibyte_secret_length_counted_in_bytes(build):
    # 16 characters, 32 bytes in UTF-8
    app = build("é" * 16)
    assert app.config["JWT_SECRET_KEY"] == "é" * 16


def test_dev_mode_generates_random_secret_per_process(build, caplog):
    with caplog.at_level(logging.WARNING):
        first = build(dev=True)
        second = build(dev=True)
    a, b = first.config["JWT_SECRET_KEY"], second.config["JWT_SECRET_KEY"]
    assert len(a) == 64 and len(b) == 64
    assert a != b
    assert "ATCS_DEV=1" in caplog.text
    assert a not in caplog.text and b not in caplog.text


def test_dev_mode_tokens_do_not_survive_restart(build):
    first = build(dev=True)
    with first.app_context():
        token = create_access_token(identity="1")
    headers = {"Authorization": f"Bearer {token}"}
    assert first.test_client().get("/api/compare?scenario_id=1", headers=headers).status_code == 200
    second = build(dev=True)
    assert second.test_client().get("/api/compare?scenario_id=1", headers=headers).status_code == 401


def test_full_auth_flow_with_configured_secret(build, caplog):
    client = build(STRONG).test_client()
    password = "flow-password-123"
    with caplog.at_level(logging.DEBUG):
        registered = client.post("/api/auth/register", json={"username": "flow", "password": password})
        assert registered.status_code == 201
        login = client.post("/api/auth/login", json={"username": "flow", "password": password})
        token = login.get_json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/compare?scenario_id=1", headers=headers).status_code == 200
        assert client.get("/api/runs/export.csv", headers=headers).status_code == 200
        assert client.get("/api/compare?scenario_id=1").status_code == 401
    for value in (STRONG, token, password):
        assert value not in caplog.text


@pytest.mark.parametrize(
    ("env", "secret", "dev"),
    [
        ({}, None, False),
        ({"JWT_SECRET_KEY": STRONG}, STRONG, False),
        ({"ATCS_DEV": "1"}, None, True),
        ({"ATCS_DEV": "true"}, None, False),
        ({"ATCS_DEV": "0"}, None, False),
    ],
)
def test_config_reads_environment(monkeypatch, env, secret, dev):
    import config

    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    monkeypatch.delenv("ATCS_DEV", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    try:
        reloaded = importlib.reload(config).Config
        assert reloaded.JWT_SECRET_KEY == secret
        assert reloaded.ATCS_DEV is dev
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_no_constant_fallback_in_source():
    import inspect

    import app as app_module
    import config

    for module in (app_module, config):
        assert FORMER_DEV_FALLBACK not in inspect.getsource(module)
