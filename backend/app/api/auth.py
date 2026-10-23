import re

from flask import current_app, request
from flask_jwt_extended import create_access_token
from sqlalchemy.exc import IntegrityError

from app import bcrypt, db
from app.api import api_bp
from app.models import User

USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,64}")
MIN_PASSWORD = 8
# bcrypt only uses the first 72 bytes of a password.
MAX_PASSWORD_BYTES = 72
INVALID_LOGIN = {"error": "invalid username or password"}

_dummy_hash = None


def dummy_hash():
    """Hash checked for unknown usernames, so both login failures cost the same."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = bcrypt.generate_password_hash("atcs-unknown-user").decode()
    return _dummy_hash


def credentials():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, None
    return data.get("username"), data.get("password")


@api_bp.route("/auth/register", methods=["POST"])
def register():
    username, password = credentials()
    errors = []
    if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
        errors.append("username must be 3 to 64 letters, digits, '.', '_' or '-'")
    if (
        not isinstance(password, str)
        or len(password) < MIN_PASSWORD
        or len(password.encode()) > MAX_PASSWORD_BYTES
    ):
        errors.append(f"password must be {MIN_PASSWORD} to {MAX_PASSWORD_BYTES} bytes long")
    if errors:
        return {"errors": errors}, 400

    if User.query.filter_by(username=username).first():
        return {"error": "username already taken"}, 409
    user = User(username=username, password_hash=bcrypt.generate_password_hash(password).decode())
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return {"error": "username already taken"}, 409
    return user.to_dict(), 201


@api_bp.route("/auth/login", methods=["POST"])
def login():
    username, password = credentials()
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        return {"error": "username and password are required"}, 400
    user = User.query.filter_by(username=username).first()
    stored = user.password_hash if user else dummy_hash()
    valid = bcrypt.check_password_hash(stored, password.encode()[:MAX_PASSWORD_BYTES])
    if user is None or not valid:
        return INVALID_LOGIN, 401
    expires = current_app.config["JWT_ACCESS_TOKEN_EXPIRES"]
    return {
        "access_token": create_access_token(identity=str(user.id)),
        "token_type": "Bearer",
        "expires_in": int(expires.total_seconds()),
    }
