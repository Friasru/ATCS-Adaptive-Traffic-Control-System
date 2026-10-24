import hashlib
import hmac
from datetime import datetime, timezone

from app import db

ROLES = ("viewer", "operator")


def account_stamp(user):
    """Fingerprint of this particular account. bcrypt hashes carry a random salt,
    so an account registered again later (same name, even the same id after the
    database was recreated or a row was reused) gets a different stamp."""
    return hashlib.sha256(user.password_hash.encode()).hexdigest()


class MonitorGrant(db.Model):
    """Monitor access, created only by the local monitor_access.py script."""

    __tablename__ = "monitor_grants"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), unique=True, nullable=False)
    username = db.Column(db.String(64), nullable=False)
    account_stamp = db.Column(db.String(64), nullable=False)
    role = db.Column(db.String(16), nullable=False)
    granted_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def matches(self, user):
        return (
            user is not None
            and self.role in ROLES
            and user.id == self.user_id
            and user.username == self.username
            and hmac.compare_digest(self.account_stamp, account_stamp(user))
        )

    def to_dict(self):
        return {
            "username": self.username,
            "role": self.role,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
        }
