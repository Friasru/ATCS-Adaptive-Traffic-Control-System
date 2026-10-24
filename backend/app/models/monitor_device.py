import hashlib
import re
import secrets
from datetime import datetime, timezone

from app import db

TOKEN_PREFIX = "atcsdev_"
TOKEN_BYTES = 32
DEVICE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{3,64}")


def utcnow():
    """Naive UTC, which is what SQLite gives back for these columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_token():
    """A fresh device credential: 32 random bytes, URL-safe, with a recognizable prefix."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token):
    """Only this hash is stored. The token is random, so a fast hash is enough."""
    return hashlib.sha256(token.encode()).hexdigest()


class MonitorDevice(db.Model):
    """A desktop ATCS Monitor allowed to send telemetry, created only by monitor_access.py."""

    __tablename__ = "monitor_devices"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), unique=True, nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    revoked_at = db.Column(db.DateTime)
    last_seen_at = db.Column(db.DateTime)

    @property
    def active(self):
        return self.revoked_at is None

    def to_dict(self):
        return {
            "name": self.name,
            "active": self.active,
            "created_at": iso(self.created_at),
            "revoked_at": iso(self.revoked_at),
            "last_seen_at": iso(self.last_seen_at),
        }


def iso(dt):
    """ISO 8601 with an explicit UTC offset for a naive-UTC column value."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds")
