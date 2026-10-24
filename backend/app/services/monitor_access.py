"""Grant, revoke and list monitor access. Used by backend/monitor_access.py only;
no HTTP endpoint can create a grant."""

from app import db
from app.models import MonitorGrant, User
from app.models.monitor_grant import ROLES, account_stamp


def grant(username, role):
    if role not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    user = User.query.filter_by(username=username).first()
    if user is None:
        raise ValueError(f"no account named {username!r}; register it first")
    existing = MonitorGrant.query.filter_by(user_id=user.id).first()
    if existing is not None:
        db.session.delete(existing)
        db.session.flush()
    db.session.add(MonitorGrant(user_id=user.id, username=user.username, account_stamp=account_stamp(user), role=role))
    db.session.commit()


def revoke(username):
    """Remove every grant held under this name or by the account with this name."""
    user = User.query.filter_by(username=username).first()
    conditions = [MonitorGrant.username == username]
    if user is not None:
        conditions.append(MonitorGrant.user_id == user.id)
    count = MonitorGrant.query.filter(db.or_(*conditions)).delete(synchronize_session=False)
    db.session.commit()
    return count


def list_grants():
    """[(grant dict, valid)]; a grant is invalid when its account no longer matches."""
    out = []
    for g in MonitorGrant.query.order_by(MonitorGrant.username):
        out.append((g.to_dict(), g.matches(db.session.get(User, g.user_id))))
    return out
