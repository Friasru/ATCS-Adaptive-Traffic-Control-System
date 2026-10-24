"""/api/monitor: JWT + allow-list access control and the proxy to the local monitor service.

The monitor service is replaced by a small standard-library HTTP stub on
127.0.0.1, so these tests need no camera, OpenCV or YOLO, and never import the
monitor's code.
"""

import json
import socket
import subprocess
import sys
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from flask_jwt_extended import create_access_token

from app import bcrypt, create_app, db
from app.models import User
from app.models import MonitorGrant
from app.services import monitor_access, monitor_client
from tests.conftest import ConfigForTesting

SERVICE_SECRET = "service-test-secret-" + "s" * 32
JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
STATUS = {"running": False, "state": "Idle", "visible": {"car": 0}}

READ = [("GET", "/api/monitor/status"), ("GET", "/api/monitor/sources"), ("GET", "/api/monitor/sessions"),
        ("GET", "/api/monitor/frame.jpg")]
WRITE = [("POST", "/api/monitor/start"), ("POST", "/api/monitor/stop"),
         ("POST", "/api/monitor/recording/start"), ("POST", "/api/monitor/recording/stop")]
ALL = READ + WRITE
VALID_START = {"source": "video:clip.mp4"}


class StubService:
    """Records requests and answers with canned responses: path -> (status, content type, body, delay)."""

    def __init__(self):
        self.requests = []
        self.responses = {}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                stub.requests.append({"method": self.command, "path": self.path,
                                      "auth": self.headers.get("Authorization"),
                                      "body": json.loads(body) if body else None})
                status, ctype, payload, delay = stub.responses.get(
                    self.path, (200, "application/json", json.dumps(STATUS).encode(), 0))
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    if self.path == "/frame.jpg":
                        self.send_header("X-Frame-Index", "42")
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            do_GET = do_POST = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def respond(self, path, status=200, payload=None, ctype="application/json", delay=0.0):
        body = payload if isinstance(payload, bytes) else json.dumps(payload if payload is not None else {}).encode()
        self.responses[path] = (status, ctype, body, delay)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = StubService()
    s.respond("/frame.jpg", payload=JPEG, ctype="image/jpeg")
    yield s
    s.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


DEFAULT_GRANTS = {"op_user": "operator", "view_user": "viewer"}


def monitor_config(port, secret=SERVICE_SECRET, timeout=2.0):
    class MonitorConfig(ConfigForTesting):
        MONITOR_SERVICE_PORT = str(port)
        MONITOR_SERVICE_SECRET = secret
        MONITOR_SERVICE_TIMEOUT_S = timeout

    return MonitorConfig


def add_user(name, password="password1"):
    user = User(username=name, password_hash=bcrypt.generate_password_hash(password).decode())
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def build(stub):
    apps = []

    def _build(grants=None, **kw):
        kw.setdefault("port", stub.port)
        app = create_app(monitor_config(**kw))
        with app.app_context():
            for name in ("op_user", "view_user", "stranger"):
                add_user(name)
            for name, role in (DEFAULT_GRANTS if grants is None else grants).items():
                monitor_access.grant(name, role)
        apps.append(app)
        return app

    yield _build
    for app in apps:
        with app.app_context():
            db.session.remove()
            db.drop_all()


@pytest.fixture
def mon_app(build):
    return build()


def token_for(app, username=None, identity=None, **kw):
    with app.app_context():
        if identity is None:
            identity = str(User.query.filter_by(username=username).one().id)
        return create_access_token(identity=identity, **kw)


def headers(app, username=None, **kw):
    return {"Authorization": f"Bearer {token_for(app, username, **kw)}"}


def send(client, method, path, hdrs=None, body=None):
    if method == "POST":
        return client.post(path, headers=hdrs, json=body if body is not None else (VALID_START if path.endswith("/start") and "recording" not in path else None))
    return client.get(path, headers=hdrs)


# ------------------------------------------------------------- 401
@pytest.mark.parametrize("method,path", ALL)
def test_no_token_is_401(mon_app, stub, method, path):
    r = send(mon_app.test_client(), method, path)
    assert r.status_code == 401 and r.get_json() == {"error": "authentication required"}
    assert stub.requests == []


@pytest.mark.parametrize("method,path", ALL)
def test_invalid_token_is_401(mon_app, stub, method, path):
    for bad in ("Bearer not-a-jwt", "Bearer " + SERVICE_SECRET, "Basic abc"):
        r = send(mon_app.test_client(), method, path, {"Authorization": bad})
        assert r.status_code == 401
    assert stub.requests == []


def test_token_signed_with_other_key_is_401(mon_app, stub):
    other = create_app(monitor_config(stub.port))
    other.config["JWT_SECRET_KEY"] = "another-test-secret-key-of-32-bytes!!"
    with other.app_context():
        forged = create_access_token(identity="1")
        db.drop_all()
    r = mon_app.test_client().get("/api/monitor/status", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401 and stub.requests == []


@pytest.mark.parametrize("method,path", ALL)
def test_expired_token_is_401(mon_app, stub, method, path):
    hdrs = headers(mon_app, "op_user", expires_delta=timedelta(seconds=-1))
    r = send(mon_app.test_client(), method, path, hdrs)
    assert r.status_code == 401 and r.get_json() == {"error": "token expired"}
    assert stub.requests == []


# ------------------------------------------------------------- 403
@pytest.mark.parametrize("method,path", ALL)
def test_unlisted_user_is_403(mon_app, stub, method, path):
    r = send(mon_app.test_client(), method, path, headers(mon_app, "stranger"))
    assert r.status_code == 403 and r.get_json() == {"error": "not authorized for the monitor"}
    assert stub.requests == []


@pytest.mark.parametrize("identity", ["999", "abc", "", "1 OR 1=1"])
def test_unknown_identity_is_403(mon_app, stub, identity):
    r = mon_app.test_client().get("/api/monitor/status", headers=headers(mon_app, identity=identity))
    assert r.status_code == 403 and stub.requests == []


@pytest.mark.parametrize("method,path", ALL)
def test_deny_by_default_without_grants(build, stub, method, path):
    app = build(grants={})
    for user in ("op_user", "view_user", "stranger"):
        assert send(app.test_client(), method, path, headers(app, user)).status_code == 403
    assert stub.requests == []


def test_environment_variables_grant_nothing(build, stub, monkeypatch):
    app = build(grants={})
    app.config["MONITOR_OPERATORS"] = app.config["MONITOR_VIEWERS"] = "stranger op_user"
    for user in ("op_user", "stranger"):
        assert app.test_client().get("/api/monitor/status", headers=headers(app, user)).status_code == 403


def test_newly_registered_account_gets_no_access(mon_app, stub):
    client = mon_app.test_client()
    assert client.post("/api/auth/register", json={"username": "newbie", "password": "password-123"}).status_code == 201
    token = client.post("/api/auth/login", json={"username": "newbie", "password": "password-123"}).get_json()["access_token"]
    hdrs = {"Authorization": f"Bearer {token}"}
    for method, path in ALL:
        assert send(client, method, path, hdrs).status_code == 403
    assert stub.requests == []


def login(client, name, password="password-123"):
    r = client.post("/api/auth/login", json={"username": name, "password": password})
    return {"Authorization": f"Bearer {r.get_json()['access_token']}"}


def test_recreated_database_does_not_inherit_grants(mon_app, stub):
    """The reported risk: atcs.db is recreated and someone registers the operator's name first."""
    client = mon_app.test_client()
    old = headers(mon_app, "op_user")
    assert client.get("/api/monitor/status", headers=old).status_code == 200
    with mon_app.app_context():
        db.drop_all()
        db.create_all()
    assert client.post("/api/auth/register", json={"username": "op_user", "password": "password-123"}).status_code == 201
    with mon_app.app_context():
        assert db.session.get(User, 1).username == "op_user"  # same name and same id as before
    for hdrs in (login(client, "op_user"), old):
        for method, path in ALL:
            assert send(client, method, path, hdrs).status_code == 403


def test_reused_user_id_with_same_name_gets_no_access(build, stub):
    """A deleted account's id and name taken over by a new registration."""
    app = build(grants={})
    with app.app_context():
        victim = add_user("victim")
        monitor_access.grant("victim", "operator")
        victim_id = victim.id
    client = app.test_client()
    assert client.post("/api/monitor/stop", headers=headers(app, "victim")).status_code == 200
    with app.app_context():
        db.session.delete(db.session.get(User, victim_id))  # grant row left behind
        db.session.commit()
    assert client.post("/api/auth/register", json={"username": "victim", "password": "password-123"}).status_code == 201
    with app.app_context():
        assert User.query.filter_by(username="victim").one().id == victim_id
        assert MonitorGrant.query.filter_by(user_id=victim_id).count() == 1
    stub.requests.clear()
    for method, path in ALL:
        assert send(client, method, path, login(client, "victim")).status_code == 403
    assert stub.requests == []


def test_grant_follows_the_account_not_the_name(build, stub):
    app = build(grants={"view_user": "viewer"})
    with app.app_context():
        grant = MonitorGrant.query.one()
        grant.username = "stranger"  # tampered or stale row
        db.session.commit()
    for user in ("view_user", "stranger"):
        assert app.test_client().get("/api/monitor/status", headers=headers(app, user)).status_code == 403


def test_changed_account_credentials_invalidate_grant(mon_app, stub):
    with mon_app.app_context():
        user = User.query.filter_by(username="op_user").one()
        user.password_hash = bcrypt.generate_password_hash("another-password").decode()
        db.session.commit()
    assert mon_app.test_client().get("/api/monitor/status", headers=headers(mon_app, "op_user")).status_code == 403


def test_unknown_role_in_database_is_denied(mon_app, stub):
    with mon_app.app_context():
        grant = MonitorGrant.query.join(User, User.id == MonitorGrant.user_id).filter(User.username == "view_user").one()
        grant.role = "admin"
        db.session.commit()
    assert mon_app.test_client().get("/api/monitor/status", headers=headers(mon_app, "view_user")).status_code == 403


def test_revoke_and_regrant(mon_app, stub):
    client = mon_app.test_client()
    with mon_app.app_context():
        assert monitor_access.revoke("op_user") == 1
    assert client.get("/api/monitor/status", headers=headers(mon_app, "op_user")).status_code == 403
    with mon_app.app_context():
        monitor_access.grant("op_user", "viewer")
    assert client.get("/api/monitor/status", headers=headers(mon_app, "op_user")).status_code == 200
    assert client.post("/api/monitor/stop", headers=headers(mon_app, "op_user")).status_code == 403
    with mon_app.app_context():
        monitor_access.grant("op_user", "operator")  # replaces, never duplicates
        assert MonitorGrant.query.count() == 2
    assert client.post("/api/monitor/stop", headers=headers(mon_app, "op_user")).status_code == 200


def test_grant_needs_existing_account_and_valid_role(mon_app):
    with mon_app.app_context():
        with pytest.raises(ValueError, match="no account"):
            monitor_access.grant("ghost", "operator")
        with pytest.raises(ValueError, match="role"):
            monitor_access.grant("stranger", "admin")
        assert MonitorGrant.query.count() == 2


def test_list_marks_stale_grants(mon_app):
    with mon_app.app_context():
        user = User.query.filter_by(username="view_user").one()
        user.password_hash = bcrypt.generate_password_hash("changed-password").decode()
        db.session.commit()
        listed = {g["username"]: (g["role"], valid) for g, valid in monitor_access.list_grants()}
    assert listed == {"op_user": ("operator", True), "view_user": ("viewer", False)}


def test_monitor_access_script(tmp_path, capsys):
    import monitor_access as script

    class FileConfig(ConfigForTesting):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "grants.db")

    app = create_app(FileConfig)
    with app.app_context():
        add_user("alberto")
    assert script.main(["grant", "alberto", "operator"], FileConfig) == 0
    assert script.main(["grant", "nobody", "viewer"], FileConfig) == 1
    assert "no account named 'nobody'" in capsys.readouterr().err
    assert script.main(["list"], FileConfig) == 0
    out = capsys.readouterr().out
    assert "alberto" in out and "operator" in out and "INVALID" not in out
    with pytest.raises(SystemExit):
        script.main(["grant", "alberto", "admin"], FileConfig)
    assert script.main(["revoke", "alberto"], FileConfig) == 0
    assert "removed 1 grant(s)" in capsys.readouterr().out
    with app.app_context():
        db.session.remove()


def test_registration_and_login_unchanged(mon_app):
    client = mon_app.test_client()
    r = client.post("/api/auth/register", json={"username": "plain", "password": "password-123"})
    assert r.status_code == 201 and set(r.get_json()) == {"id", "username"}
    r = client.post("/api/auth/login", json={"username": "plain", "password": "password-123"})
    assert r.status_code == 200 and set(r.get_json()) == {"access_token", "token_type", "expires_in"}
    with mon_app.app_context():
        assert MonitorGrant.query.count() == 2  # registering created no grant


def test_client_submitted_role_is_ignored(mon_app, stub):
    client = mon_app.test_client()
    forged = headers(mon_app, "view_user", additional_claims={"role": "operator", "is_admin": True})
    forged["X-Role"] = "operator"
    r = client.post("/api/monitor/start", headers=forged, json={**VALID_START, "role": "operator"})
    assert r.status_code == 403
    r = client.get("/api/monitor/status?role=operator", headers=headers(mon_app, "stranger", additional_claims={"role": "operator"}))
    assert r.status_code == 403
    assert stub.requests == []


# ------------------------------------------------------- viewer / operator
@pytest.mark.parametrize("method,path", READ)
def test_viewer_can_read(mon_app, stub, method, path):
    r = send(mon_app.test_client(), method, path, headers(mon_app, "view_user"))
    assert r.status_code == 200


@pytest.mark.parametrize("method,path", WRITE)
def test_viewer_cannot_control(mon_app, stub, method, path):
    r = send(mon_app.test_client(), method, path, headers(mon_app, "view_user"))
    assert r.status_code == 403 and stub.requests == []


@pytest.mark.parametrize("method,path,service_path,expected", [
    ("GET", "/api/monitor/status", "/status", 200),
    ("GET", "/api/monitor/sources", "/sources", 200),
    ("GET", "/api/monitor/sessions", "/sessions", 200),
    ("POST", "/api/monitor/start", "/start", 202),
    ("POST", "/api/monitor/stop", "/stop", 200),
    ("POST", "/api/monitor/recording/start", "/recording/start", 202),
    ("POST", "/api/monitor/recording/stop", "/recording/stop", 202),
])
def test_operator_full_access_and_forwarding(mon_app, stub, method, path, service_path, expected):
    r = send(mon_app.test_client(), method, path, headers(mon_app, "op_user"))
    assert r.status_code == expected and r.get_json() == STATUS
    req = stub.requests[-1]
    assert (req["method"], req["path"]) == (method, service_path)
    assert req["auth"] == f"Bearer {SERVICE_SECRET}"
    if service_path == "/start":
        assert req["body"] == {"source": "video:clip.mp4", "loop": False}
    elif method == "POST":
        assert req["body"] == {}


def test_frame_proxy(mon_app, stub):
    r = mon_app.test_client().get("/api/monitor/frame.jpg", headers=headers(mon_app, "view_user"))
    assert r.status_code == 200 and r.mimetype == "image/jpeg" and r.data == JPEG
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Frame-Index"] == "42"


def test_operators_are_also_viewers(build, stub):
    app = build(grants={"op_user": "operator"})
    assert app.test_client().get("/api/monitor/status", headers=headers(app, "op_user")).status_code == 200


# ---------------------------------------------------------- validation
@pytest.mark.parametrize("body", [
    {}, {"source": ""}, {"source": "/etc/passwd"}, {"source": "video:../secret.mp4"}, {"source": "video:a/b.mp4"},
    {"source": "C:\\Videos\\x.mp4"}, {"source": "file:clip.mp4"}, {"source": "camera:1234"}, {"source": 3},
    {"source": "video:clip.mp4", "loop": "true"}, {"source": "video:clip.mp4", "path": "/tmp"},
    {"source": "video:" + "a" * 200},
])
def test_start_rejects_invalid_bodies(mon_app, stub, body):
    r = mon_app.test_client().post("/api/monitor/start", headers=headers(mon_app, "op_user"), json=body)
    assert r.status_code == 400 and r.get_json()["code"] in ("invalid_source", "invalid_body")
    assert stub.requests == []


@pytest.mark.parametrize("data,ctype", [("not json", "application/json"), ("[1]", "application/json"),
                                        ("source=video:clip.mp4", "application/x-www-form-urlencoded"),
                                        (json.dumps({"source": "video:" + "a" * 5000}), "application/json")])
def test_start_rejects_non_object_bodies(mon_app, stub, data, ctype):
    hdrs = {**headers(mon_app, "op_user"), "Content-Type": ctype}
    r = mon_app.test_client().post("/api/monitor/start", headers=hdrs, data=data)
    assert r.status_code == 400 and stub.requests == []


@pytest.mark.parametrize("path", ["/api/monitor/stop", "/api/monitor/recording/start", "/api/monitor/recording/stop"])
def test_control_endpoints_take_no_parameters(mon_app, stub, path):
    client = mon_app.test_client()
    hdrs = headers(mon_app, "op_user")
    assert client.post(path, headers=hdrs, json={"path": "/etc"}).status_code == 400
    assert client.post(path, headers={**hdrs, "Content-Type": "application/json"}, data="nope").status_code == 400
    assert stub.requests == []
    assert client.post(path, headers=hdrs, json={}).status_code in (200, 202)
    assert client.post(path, headers=hdrs).status_code in (200, 202)


@pytest.mark.parametrize("method,path", [("GET", "/api/monitor/start"), ("POST", "/api/monitor/status"),
                                         ("DELETE", "/api/monitor/stop"), ("PUT", "/api/monitor/frame.jpg")])
def test_wrong_method_is_405(mon_app, stub, method, path):
    r = mon_app.test_client().open(path, method=method, headers=headers(mon_app, "op_user"))
    assert r.status_code == 405 and r.headers["Allow"] and stub.requests == []


@pytest.mark.parametrize("path", ["/api/monitor/../../etc/passwd", "/api/monitor/recordings/x.mp4",
                                  "/api/monitor/frame.jpg/../../x", "/api/monitor/download?file=/etc/passwd"])
def test_no_file_or_download_routes(mon_app, stub, path):
    r = mon_app.test_client().get(path, headers=headers(mon_app, "op_user"))
    assert r.status_code == 404 and stub.requests == []


# ------------------------------------------------------- service problems
@pytest.mark.parametrize("secret,port", [(None, "8765"), ("", "8765"), ("short", "8765"), ("x" * 31, "8765"),
                                         (SERVICE_SECRET, "80"), (SERVICE_SECRET, "abc"), (SERVICE_SECRET, "")])
def test_not_configured_is_503(build, stub, secret, port):
    app = build(secret=secret, port=port)
    for method, path in ALL:
        r = send(app.test_client(), method, path, headers(app, "op_user"))
        assert r.status_code == 503
        assert r.get_json() == {"error": "monitor service is not configured", "code": "service_not_configured"}
    assert stub.requests == []


def test_unconfigured_service_still_checks_access_first(build, stub):
    app = build(secret=None)
    assert app.test_client().get("/api/monitor/status", headers=headers(app, "stranger")).status_code == 403
    assert app.test_client().get("/api/monitor/status").status_code == 401


@pytest.mark.parametrize("method,path", ALL)
def test_service_down_is_503(build, stub, method, path):
    app = build(port=free_port())
    r = send(app.test_client(), method, path, headers(app, "op_user"))
    assert r.status_code == 503
    assert r.get_json() == {"error": "monitor service is not running", "code": "service_unavailable"}


def test_service_timeout_is_504(build, stub):
    app = build(timeout=0.3)
    stub.respond("/status", payload=STATUS, delay=1.5)
    t0 = time.monotonic()
    r = app.test_client().get("/api/monitor/status", headers=headers(app, "view_user"))
    assert r.status_code == 504 and r.get_json()["code"] == "service_timeout"
    assert time.monotonic() - t0 < 1.4


def test_stop_uses_longer_timeout(build, stub, monkeypatch):
    app = build(timeout=0.2)
    stub.respond("/stop", payload=STATUS, delay=0.6)
    r = app.test_client().post("/api/monitor/stop", headers=headers(app, "op_user"))
    assert r.status_code == 200


@pytest.mark.parametrize("status,payload,expected,code", [
    (409, {"error": "monitoring is already running; stop it first", "code": "already_running"}, 409, "already_running"),
    (400, {"error": "source is not in the allowed source list", "code": "unknown_source"}, 400, "unknown_source"),
    (404, {"error": "no frame is available yet", "code": "no_frame"}, 404, "no_frame"),
    (503, {"error": "model weights 'yolo26n.pt' are not installed", "code": "model_unavailable"}, 503, "model_unavailable"),
    (503, {"error": "too many concurrent requests", "code": "busy"}, 503, "busy"),
    (500, {"error": "Traceback /home/alberto/monitor/app/x.py", "code": "internal_error"}, 502, "service_error"),
    (503, {"error": "weird /home/alberto", "code": "something_else"}, 502, "service_error"),
    (409, {"error": "no code"}, 502, "service_error"),
    (401, {"error": "missing or invalid service token", "code": "unauthorized"}, 502, "service_auth_failed"),
    (403, {}, 502, "service_auth_failed"),
])
def test_service_errors_are_mapped(mon_app, stub, status, payload, expected, code):
    stub.respond("/start", status=status, payload=payload)
    r = mon_app.test_client().post("/api/monitor/start", headers=headers(mon_app, "op_user"), json=VALID_START)
    assert r.status_code == expected and r.get_json()["code"] == code
    assert "/home/" not in r.get_data(as_text=True)


@pytest.mark.parametrize("payload,ctype", [(b"<html>oops</html>", "text/html"), (b"not json", "application/json"),
                                           (b"[1, 2]", "application/json")])
def test_bad_service_json_is_502(mon_app, stub, payload, ctype):
    stub.respond("/status", payload=payload, ctype=ctype)
    r = mon_app.test_client().get("/api/monitor/status", headers=headers(mon_app, "view_user"))
    assert r.status_code == 502 and r.get_json()["code"] == "bad_service_response"


def test_bad_frame_responses_are_502(mon_app, stub, monkeypatch):
    client, hdrs = mon_app.test_client(), headers(mon_app, "view_user")
    stub.respond("/frame.jpg", payload=b"{}", ctype="application/json")
    assert client.get("/api/monitor/frame.jpg", headers=hdrs).status_code == 502
    stub.respond("/frame.jpg", payload=b"\xff\xd8" + b"x" * 100, ctype="image/jpeg")
    monkeypatch.setattr(monitor_client, "MAX_FRAME_BYTES", 50)
    assert client.get("/api/monitor/frame.jpg", headers=hdrs).status_code == 502


def test_service_auth_failure_does_not_log_out_the_user(mon_app, stub):
    """A wrong service secret must not look like an expired JWT (401) to the dashboard."""
    stub.respond("/status", status=401, payload={"error": "missing or invalid service token", "code": "unauthorized"})
    r = mon_app.test_client().get("/api/monitor/status", headers=headers(mon_app, "view_user"))
    assert r.status_code == 502


# ------------------------------------------------------------ secrecy
def test_service_secret_never_reaches_the_client(mon_app, stub, caplog):
    client = mon_app.test_client()
    responses = [send(client, m, p, headers(mon_app, "op_user")) for m, p in ALL]
    stub.respond("/status", status=401, payload={})
    responses.append(client.get("/api/monitor/status", headers=headers(mon_app, "op_user")))
    for r in responses:
        assert SERVICE_SECRET.encode() not in r.get_data()
        assert SERVICE_SECRET not in str(r.headers)
    assert SERVICE_SECRET not in caplog.text


def test_frontend_has_no_monitor_secret():
    frontend = Path(__file__).resolve().parents[2] / "frontend"
    for f in frontend.rglob("*"):
        if f.is_file() and f.suffix in (".html", ".js", ".css"):
            assert "ATCS_MONITOR_SERVICE_SECRET" not in f.read_text(encoding="utf-8", errors="ignore")


def test_only_localhost_is_contacted():
    assert monitor_client.HOST == "127.0.0.1"


# ------------------------------------------------------------- isolation
def test_backend_does_not_import_monitor_code():
    code = (
        "import sys; from app import create_app; import app.api.monitor, app.services.monitor_client; "
        "import app as pkg; "
        "bad = [m for m in ('cv2', 'torch', 'ultralytics', 'numpy', 'PySide6', 'web_service') if m in sys.modules]; "
        "print(pkg.__file__); print(bad)"
    )
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    pkg_file, bad = out.stdout.strip().splitlines()
    assert Path(pkg_file).resolve().parent.parent == backend
    assert bad == "[]"
    assert "monitor" not in [Path(p).name for p in sys.path]


def test_existing_routes_unchanged(mon_app):
    client = mon_app.test_client()
    assert client.get("/api/health").get_json() == {"status": "ok"}
    assert client.get("/api/compare?scenario_id=1").status_code == 401
