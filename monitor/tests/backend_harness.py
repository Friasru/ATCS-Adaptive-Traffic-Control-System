"""Runs the real ATCS backend for test_desktop_link_backend.py (run with the backend's Python).

    python backend_harness.py serve <db> <out.json> [port]   start Flask on 127.0.0.1
    python backend_harness.py revoke <db> <device>            revoke a device token

`serve` creates a viewer account and two devices in a fresh temporary database
and writes the port, the device tokens and a viewer JWT to <out.json> (mode
0600, inside the test's temporary folder). Rate limits can be lowered with
ATCS_TEST_RATE / ATCS_TEST_BURST.
"""

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from config import Config  # noqa: E402


def make_config(db):
    class HarnessConfig(Config):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(db)
        JWT_SECRET_KEY = os.environ.get("ATCS_TEST_JWT_SECRET", "harness-only-secret-value-of-sufficient-length-0123")
        BCRYPT_LOG_ROUNDS = 4
        DESKTOP_RATE_PER_S = float(os.environ.get("ATCS_TEST_RATE", Config.DESKTOP_RATE_PER_S))
        DESKTOP_RATE_BURST = float(os.environ.get("ATCS_TEST_BURST", Config.DESKTOP_RATE_BURST))

    return HarnessConfig


def serve(db, out, port=0):
    from flask_jwt_extended import create_access_token
    from werkzeug.serving import make_server

    from app import bcrypt, create_app, db as sqla
    from app.models import User
    from app.services import monitor_access, monitor_devices

    app = create_app(make_config(db))
    info = {}
    with app.app_context():
        user = User.query.filter_by(username="viewer").first()
        if user is None:
            user = User(username="viewer", password_hash=bcrypt.generate_password_hash("password1").decode())
            sqla.session.add(user)
            sqla.session.commit()
            monitor_access.grant("viewer", "viewer")
            info["tokens"] = {name: monitor_devices.add(name) for name in ("desk-a", "desk-b")}
        info["jwt"] = create_access_token(identity=str(user.id))
    srv = make_server("127.0.0.1", int(port), app, threaded=True)
    info["port"] = srv.server_port
    if Path(out).exists():
        info = {**json.loads(Path(out).read_text()), **info}
    fd = os.open(out + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(info, fh)
    os.replace(out + ".tmp", out)
    srv.serve_forever()


def revoke(db, name):
    from app import create_app
    from app.services import monitor_devices

    app = create_app(make_config(db))
    with app.app_context():
        monitor_devices.revoke(name)


if __name__ == "__main__":
    if sys.argv[1] == "serve":
        serve(*sys.argv[2:])
    elif sys.argv[1] == "revoke":
        revoke(*sys.argv[2:])
    else:
        sys.exit(f"unknown command {sys.argv[1]!r}")
