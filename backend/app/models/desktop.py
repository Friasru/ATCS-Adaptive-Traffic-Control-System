"""Telemetry received from desktop ATCS Monitor apps.

Two kinds of time are kept apart everywhere: columns named *_received_at /
received_at / finalized_at are this server's clock (naive UTC), while the
desktop's own timestamps are stored exactly as it sent them (ISO 8601 strings
with an offset), plus a parsed UTC copy where the server needs to sort or compare.
"""

import json

from app import db
from app.models.monitor_device import iso, utcnow


def _load(text, default):
    return json.loads(text) if text else default


class DesktopStatus(db.Model):
    """The last accepted heartbeat of each device (one row per device)."""

    __tablename__ = "desktop_status"

    device_id = db.Column(db.Integer, db.ForeignKey("monitor_devices.id"), primary_key=True)
    instance_id = db.Column(db.String(32), nullable=False)
    instance_started_at_utc = db.Column(db.DateTime, nullable=False)  # desktop clock, parsed
    seq = db.Column(db.BigInteger, nullable=False)
    desktop_sent_at = db.Column(db.String(40), nullable=False)
    desktop_sent_at_utc = db.Column(db.DateTime, nullable=False)
    received_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    document_json = db.Column(db.Text, nullable=False)

    @property
    def document(self):
        return _load(self.document_json, {})


class DesktopSession(db.Model):
    """One monitoring session of one device. session_id is the desktop's own id and
    is unique across all devices: a device can never write to another device's session."""

    __tablename__ = "desktop_sessions"

    session_id = db.Column(db.String(32), primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("monitor_devices.id"), nullable=False, index=True)
    instance_id = db.Column(db.String(32))  # desktop process that reported it live; None if only uploaded
    app_version = db.Column(db.String(32))
    source_type = db.Column(db.String(16), nullable=False)
    source_id = db.Column(db.String(128), nullable=False)
    started_at = db.Column(db.String(40), nullable=False)  # desktop clock, as sent
    started_at_utc = db.Column(db.DateTime, nullable=False, index=True)
    ended_at = db.Column(db.String(40))  # desktop clock, as sent
    # running until a summary arrives; then completed | interrupted | error
    status = db.Column(db.String(16), nullable=False, default="running")
    # live | summary | last_live_update | incomplete: where unique/line totals/frames came from
    counts_origin = db.Column(db.String(20), nullable=False, default="live")
    unique_json = db.Column(db.Text, nullable=False)
    crossings_json = db.Column(db.Text, nullable=False)
    line_totals_json = db.Column(db.Text, nullable=False, default="{}")
    visible_json = db.Column(db.Text)  # last instantaneous view while live
    frames_processed = db.Column(db.BigInteger, nullable=False, default=0)
    live_seq = db.Column(db.BigInteger)
    live_timestamp = db.Column(db.String(40))  # desktop clock of the last live update
    duration_s = db.Column(db.Float)
    avg_inference_fps = db.Column(db.Float)
    model_json = db.Column(db.Text)
    recordings_json = db.Column(db.Text, nullable=False, default="[]")
    crossing_event_count = db.Column(db.Integer)  # from the summary
    summary_json = db.Column(db.Text)  # canonical normalized summary; immutable once set
    summary_sha256 = db.Column(db.String(64))
    first_received_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_received_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    finalized_at = db.Column(db.DateTime)

    device = db.relationship("MonitorDevice")

    @property
    def finalized(self):
        return self.finalized_at is not None

    def to_dict(self, detail=False):
        out = {
            "session_id": self.session_id,
            "device": self.device.name if self.device else None,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "status": self.status,
            "finalized": self.finalized,
            "counts_origin": self.counts_origin,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_s": self.duration_s,
            "frames_processed": self.frames_processed,
            "avg_inference_fps": self.avg_inference_fps,
            "unique": _load(self.unique_json, {}),
            "crossings": _load(self.crossings_json, {}),
            "recordings": len(_load(self.recordings_json, [])),
            "first_received_at": iso(self.first_received_at),
            "last_received_at": iso(self.last_received_at),
            "finalized_at": iso(self.finalized_at),
        }
        if detail:
            out.update({
                "line_totals": _load(self.line_totals_json, {}),
                "recordings": _load(self.recordings_json, []),
                "model": _load(self.model_json, None),
                "crossing_event_count": self.crossing_event_count,
                "provenance": {"device": out["device"], "instance_id": self.instance_id,
                               "app_version": self.app_version, "live_timestamp": self.live_timestamp},
            })
        return out


class DesktopCrossingEvent(db.Model):
    """Crossing events, idempotent on the contract key (session, line, track, direction)."""

    __tablename__ = "desktop_crossing_events"
    __table_args__ = (db.UniqueConstraint("session_id", "line_id", "track_id", "direction",
                                          name="uq_desktop_crossing_event"),)

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(32), db.ForeignKey("desktop_sessions.session_id"), nullable=False, index=True)
    ts = db.Column(db.String(40), nullable=False)  # desktop clock, as sent
    line_id = db.Column(db.String(64), nullable=False)
    track_id = db.Column(db.BigInteger, nullable=False)
    cls = db.Column(db.String(16), nullable=False)
    direction = db.Column(db.String(8), nullable=False)
    received_at = db.Column(db.DateTime, nullable=False, default=utcnow)
