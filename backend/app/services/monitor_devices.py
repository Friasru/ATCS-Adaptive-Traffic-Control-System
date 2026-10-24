"""Register, revoke and list desktop monitor devices. Used by backend/monitor_access.py
only; no HTTP endpoint can create a device or issue a credential.

Only the SHA-256 of a device token is stored. The token itself is returned once,
by add(), and never logged.
"""

import hmac

from sqlalchemy.exc import IntegrityError

from app import db
from app.models import MonitorDevice
from app.models.monitor_device import DEVICE_NAME_RE, TOKEN_PREFIX, hash_token, new_token, utcnow

# Longest token that could be one of ours; anything longer is rejected before hashing.
MAX_TOKEN_CHARS = 128


def add(name):
    """Create a device and return its token (shown to the administrator once)."""
    if not isinstance(name, str) or not DEVICE_NAME_RE.fullmatch(name):
        raise ValueError("device name must be 3 to 64 letters, digits, '.', '_' or '-'")
    existing = MonitorDevice.query.filter_by(name=name).first()
    if existing is not None:
        state = "an active" if existing.active else "a revoked"
        raise ValueError(f"{state} device named {name!r} already exists; choose another name")
    token = new_token()
    db.session.add(MonitorDevice(name=name, token_hash=hash_token(token)))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValueError(f"a device named {name!r} already exists; choose another name")
    return token


def revoke(name):
    """Revoke the device; its token stops working at once. Its stored sessions are kept."""
    device = MonitorDevice.query.filter_by(name=name).first()
    if device is None or not device.active:
        return False
    device.revoked_at = utcnow()
    db.session.commit()
    return True


def list_devices():
    return [d.to_dict() for d in MonitorDevice.query.order_by(MonitorDevice.name)]


def authenticate(token):
    """The active device holding this token, or None."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX) or len(token) > MAX_TOKEN_CHARS:
        return None
    digest = hash_token(token)
    device = MonitorDevice.query.filter_by(token_hash=digest).first()
    if device is None or not hmac.compare_digest(device.token_hash, digest) or not device.active:
        return None
    return device
