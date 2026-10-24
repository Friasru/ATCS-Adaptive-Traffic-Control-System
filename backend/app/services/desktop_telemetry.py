"""Store validated desktop telemetry and build the read-only views of it.

The device always comes from the authenticated credential, never from a
document. Session ids are unique across devices: a document naming another
device's session is refused. Writes are serialized by one lock, so the
read-check-write steps below cannot interleave (the backend runs as one process;
see monitor/docs/desktop_telemetry.md).

Counters are treated by kind:

* cumulative (unique, crossings, line totals, frames processed) may never go
  down within a session; a live update that lowers one is refused;
* instantaneous (visible objects, FPS, latency, active tracks, dropped frames)
  are stored as they come and may go up or down freely.
"""

import hashlib
import json
import threading
from datetime import timedelta, timezone

from app import db
from app.models import DesktopCrossingEvent, DesktopSession, DesktopStatus
from app.models.monitor_device import iso, utcnow
from app.services import desktop_schema as schema

WRITE_LOCK = threading.Lock()
# A crossing can be committed a moment after the session row was opened on the desktop.
EVENT_BEFORE_START_TOLERANCE = timedelta(seconds=2)
RUNNING_STATES = ("Initializing", "Detecting", "Recording", "Stopping")


class TelemetryError(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def to_response(self):
        return {"error": self.message, "code": self.code, **self.extra}, self.status


def invalid(exc):
    return TelemetryError(400, "invalid_document", str(exc)[:300])


def naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def check_clock(dt, name, now, skew):
    """Desktop timestamps may not be in the server's future (beyond the allowed skew)."""
    if naive_utc(dt) > now + skew:
        raise TelemetryError(422, "clock_skew", f"'{name}' is in the future; check the desktop clock")


def _dump(value):
    return json.dumps(value, sort_keys=True)


def _decreased(old, new):
    """Names of cumulative values that are lower in `new` than in `old`."""
    out = [f"unique.{c}" for c in schema.CLASSES if new["unique"][c] < old["unique"].get(c, 0)]
    out += [f"crossings.{c}" for c in schema.CLASSES if new["crossings"][c] < old["crossings"].get(c, 0)]
    for line_id, per_dir in old["line_totals"].items():
        for d, cls_counts in per_dir.items():
            for c, v in cls_counts.items():
                if new["line_totals"].get(line_id, {}).get(d, {}).get(c, 0) < v:
                    out.append(f"line_totals.{line_id}.{d}.{c}")
    if new["frames_processed"] < old["frames_processed"]:
        out.append("frames_processed")
    return out


def _stored(s):
    return {"unique": json.loads(s.unique_json), "crossings": json.loads(s.crossings_json),
            "line_totals": json.loads(s.line_totals_json), "frames_processed": s.frames_processed}


def _own_session(device, session_id):
    s = db.session.get(DesktopSession, session_id)
    if s is not None and s.device_id != device.id:
        raise TelemetryError(409, "session_conflict", "this session id belongs to another device")
    return s


def _same_session(s, started_at, source_type, source_id):
    if s.started_at != started_at or s.source_type != source_type or s.source_id != source_id:
        raise TelemetryError(409, "session_mismatch", "start time or source differ from the stored session")


def _new_session(device, session_id, started_at, started_dt, source_type, source_id, now, instance_id=None,
                 app_version=None):
    s = DesktopSession(
        session_id=session_id, device_id=device.id, instance_id=instance_id, app_version=app_version,
        source_type=source_type, source_id=source_id, started_at=started_at, started_at_utc=naive_utc(started_dt),
        status="running", counts_origin="live", unique_json=_dump(schema.zero_counts()),
        crossings_json=_dump(schema.zero_counts()), line_totals_json="{}", frames_processed=0,
        recordings_json="[]", first_received_at=now, last_received_at=now,
    )
    db.session.add(s)
    return s


# ---------------------------------------------------------------- status
def ingest_status(device, raw, config):
    try:
        doc, times = schema.validate_status(raw)
    except schema.SchemaError as exc:
        raise invalid(exc)
    now = utcnow()
    skew = timedelta(seconds=config["DESKTOP_MAX_CLOCK_SKEW_S"])
    timeout = timedelta(seconds=config["DESKTOP_HEARTBEAT_TIMEOUT_S"])
    for name, dt in times.items():
        check_clock(dt, name, now, skew)
    sent_utc = naive_utc(times["sent_at"])
    if sent_utc < now - skew:
        raise TelemetryError(422, "clock_skew", "'sent_at' is too far in the past; check the desktop clock")
    inst_started = naive_utc(times["instance_started_at"])

    with WRITE_LOCK:
        row = db.session.get(DesktopStatus, device.id)
        if row is not None:
            if row.instance_id == doc["instance_id"]:
                if doc["seq"] <= row.seq:
                    return {"accepted": False, "reason": "stale"}
            else:
                # Another process of this device. The one started later wins; an
                # older one is accepted again only once the newer one has gone quiet.
                current_alive = now - row.received_at <= timeout
                if current_alive and inst_started <= row.instance_started_at_utc:
                    return {"accepted": False, "reason": "superseded"}

        session_note = None
        sess = doc["session"]
        if sess is not None:
            session_note = _live_session(device, doc, sess, times, now)

        if row is None:
            row = DesktopStatus(device_id=device.id)
            db.session.add(row)
        row.instance_id = doc["instance_id"]
        row.instance_started_at_utc = inst_started
        row.seq = doc["seq"]
        row.desktop_sent_at = doc["sent_at"]
        row.desktop_sent_at_utc = sent_utc
        row.received_at = now
        row.document_json = _dump(doc)
        device.last_seen_at = now
        db.session.commit()
    out = {"accepted": True, "server_time": iso(now), "heartbeat_timeout_s": config["DESKTOP_HEARTBEAT_TIMEOUT_S"]}
    if session_note:
        out["session"] = session_note
    return out


def _live_session(device, doc, sess, times, now):
    s = _own_session(device, sess["session_id"])
    source = doc["source"]
    if s is None:
        s = _new_session(device, sess["session_id"], sess["started_at"], times["session.started_at"], source["kind"],
                         source["label"], now, doc["instance_id"], doc["app"]["version"])
    elif s.finalized:
        return "finalized"  # a heartbeat that arrived after the summary changes nothing
    else:
        _same_session(s, sess["started_at"], source["kind"], source["label"])
        if s.instance_id is None:
            s.instance_id = doc["instance_id"]
        elif s.instance_id != doc["instance_id"]:
            raise TelemetryError(409, "session_instance_mismatch",
                                 "this session was reported by another desktop process")
        lowered = _decreased(_stored(s), sess)
        if lowered:
            raise TelemetryError(422, "counter_decreased", "cumulative counters went down: "
                                 + ", ".join(lowered)[:200])
    s.unique_json = _dump(sess["unique"])
    s.crossings_json = _dump(sess["crossings"])
    s.line_totals_json = _dump(sess["line_totals"])
    s.visible_json = _dump(sess["visible"])
    s.frames_processed = sess["frames_processed"]
    s.live_seq = doc["seq"]
    s.live_timestamp = sess["timestamp"]
    s.last_received_at = now
    return "live"


# ---------------------------------------------------------------- events
def _event_tally(session_id):
    rows = db.session.query(DesktopCrossingEvent.cls, db.func.count()).filter_by(session_id=session_id) \
        .group_by(DesktopCrossingEvent.cls).all()
    tally = schema.zero_counts()
    for cls, n in rows:
        tally[cls] = n
    return tally


def ingest_events(device, session_id, raw, config):
    try:
        doc, started_dt, event_times = schema.validate_events(raw)
    except schema.SchemaError as exc:
        raise invalid(exc)
    if doc["session_id"] != session_id:
        raise TelemetryError(400, "invalid_document", "'session_id' does not match the URL")
    now = utcnow()
    skew = timedelta(seconds=config["DESKTOP_MAX_CLOCK_SKEW_S"])
    check_clock(started_dt, "started_at", now, skew)
    for i, dt in enumerate(event_times):
        check_clock(dt, f"events[{i}].timestamp", now, skew)
        if dt < started_dt - EVENT_BEFORE_START_TOLERANCE:
            raise TelemetryError(422, "invalid_timestamp", f"events[{i}] is before the session started")

    with WRITE_LOCK:
        s = _own_session(device, session_id)
        if s is None:
            # Uploaded after the fact (the desktop was offline): the summary follows.
            s = _new_session(device, session_id, doc["started_at"], started_dt, doc["source"], doc["source_id"], now)
            db.session.flush()
        else:
            _same_session(s, doc["started_at"], doc["source"], doc["source_id"])
        track_ids = sorted({e["track_id"] for e in doc["events"]})
        existing = {}
        for e in DesktopCrossingEvent.query.filter(DesktopCrossingEvent.session_id == session_id,
                                                   DesktopCrossingEvent.track_id.in_(track_ids)):
            existing[(e.line_id, e.track_id, e.direction)] = (e.cls, e.ts)
        new, duplicates = [], 0
        for e in doc["events"]:
            key = (e["line_id"], e["track_id"], e["direction"])
            if key in existing:
                if existing[key] != (e["class"], e["timestamp"]):
                    raise TelemetryError(409, "event_conflict",
                                         "an event with this line, track and direction is stored with other values")
                duplicates += 1
            else:
                new.append(e)
        if new and s.finalized:
            raise TelemetryError(409, "session_finalized", "the session is finalized; no events can be added")
        for e in new:
            db.session.add(DesktopCrossingEvent(session_id=session_id, ts=e["timestamp"], line_id=e["line_id"],
                                                track_id=e["track_id"], cls=e["class"], direction=e["direction"],
                                                received_at=now))
        s.last_received_at = now
        db.session.commit()
        stored = DesktopCrossingEvent.query.filter_by(session_id=session_id).count()
    return {"inserted": len(new), "duplicates": duplicates, "stored_events": stored}


# --------------------------------------------------------------- summary
def ingest_summary(device, session_id, raw, config):
    """(response, created). Finalized summaries are immutable."""
    try:
        doc, started_dt, ended_dt = schema.validate_summary(raw)
    except schema.SchemaError as exc:
        raise invalid(exc)
    if doc["session_id"] != session_id:
        raise TelemetryError(400, "invalid_document", "'session_id' does not match the URL")
    now = utcnow()
    skew = timedelta(seconds=config["DESKTOP_MAX_CLOCK_SKEW_S"])
    check_clock(started_dt, "started_at", now, skew)
    check_clock(ended_dt, "ended_at", now, skew)
    text = schema.canonical(doc)
    digest = hashlib.sha256(text.encode()).hexdigest()

    with WRITE_LOCK:
        s = _own_session(device, session_id)
        if s is not None and s.finalized:
            if s.summary_sha256 == digest:
                return {"finalized": True, "duplicate": True, "status": s.status}, False
            raise TelemetryError(409, "summary_conflict", "a different summary is already stored for this session")
        if s is not None:
            _same_session(s, doc["started_at"], doc["source"], doc["source_id"])
        stored = DesktopCrossingEvent.query.filter_by(session_id=session_id).count() if s is not None else 0
        tally = _event_tally(session_id) if s is not None else schema.zero_counts()
        if stored != doc["crossing_event_count"] or tally != doc["crossings"]:
            raise TelemetryError(409, "events_incomplete",
                                 "upload all crossing events of this session before its summary",
                                 stored_events=stored)
        if s is None:
            s = _new_session(device, session_id, doc["started_at"], started_dt, doc["source"], doc["source_id"], now)
        had_live = s.live_seq is not None
        if doc["stats_complete"]:
            final = {"unique": doc["unique"], "crossings": doc["crossings"], "line_totals": doc["line_totals"],
                     "frames_processed": doc["frames_processed"]}
            if had_live:
                lowered = _decreased(_stored(s), final)
                if lowered:
                    raise TelemetryError(422, "counter_decreased", "summary is lower than the live counts: "
                                         + ", ".join(lowered)[:200])
            s.unique_json = _dump(doc["unique"])
            s.line_totals_json = _dump(doc["line_totals"])
            s.frames_processed = doc["frames_processed"]
            s.counts_origin = "summary"
        elif had_live:
            # The desktop ended without saving its statistics: keep the last live
            # values for unique objects, line totals and frames.
            s.counts_origin = "last_live_update"
        else:
            s.unique_json = _dump(doc["unique"])
            s.line_totals_json = _dump(doc["line_totals"])
            s.frames_processed = doc["frames_processed"]
            s.counts_origin = "incomplete"
        # Crossing totals always come from the summary, which matches the stored events.
        s.crossings_json = _dump(doc["crossings"])
        s.status = doc["status"]
        s.ended_at = doc["ended_at"]
        s.duration_s = doc["duration_s"]
        s.avg_inference_fps = doc["avg_inference_fps"]
        s.model_json = _dump(doc["model"])
        s.recordings_json = _dump(doc["recordings"])
        s.crossing_event_count = doc["crossing_event_count"]
        s.visible_json = None
        s.summary_json = text
        s.summary_sha256 = digest
        s.finalized_at = now
        s.last_received_at = now
        db.session.commit()
        return {"finalized": True, "duplicate": False, "status": s.status, "counts_origin": s.counts_origin}, True


# ----------------------------------------------------------------- reads
def _totals(visible, unique, crossings):
    def total(d, classes):
        return sum(d[c] for c in classes)

    return {
        "visible_vehicles": total(visible, schema.VEHICLE_CLASSES),
        "unique_vehicles": total(unique, schema.VEHICLE_CLASSES),
        "crossing_vehicles": total(crossings, schema.VEHICLE_CLASSES),
        "visible_pedestrians": total(visible, schema.PEDESTRIAN_CLASSES),
        "unique_pedestrians": total(unique, schema.PEDESTRIAN_CLASSES),
        "crossing_pedestrians": total(crossings, schema.PEDESTRIAN_CLASSES),
    }


def status_view(device, row, now, timeout_s):
    """One device's status, with the field names of the Phase 4 status document."""
    doc = row.document if row is not None else None
    age = (now - row.received_at).total_seconds() if row is not None else None
    live = age is not None and age <= timeout_s
    connection = "never_connected" if row is None else ("connected" if live else "disconnected")
    zero = schema.zero_counts()
    sess = (doc or {}).get("session")
    visible = sess["visible"] if sess else zero
    unique = sess["unique"] if sess else zero
    crossings = sess["crossings"] if sess else zero
    perf = (doc or {}).get("performance") or {k: 0 for k in schema.PERFORMANCE_FIELDS}
    rec = (doc or {}).get("recording") or {"active": False, "file": None}
    state = doc["state"] if doc else None
    return {
        "desktop": {
            "name": device.name,
            "connection": connection,
            "last_seen_at": iso(row.received_at) if row is not None else None,
            "seconds_since_seen": round(age, 1) if age is not None else None,
            "app": doc["app"] if doc else None,
            "desktop_sent_at": doc["sent_at"] if doc else None,
        },
        "live": live,
        "running": live and state in RUNNING_STATES,
        "state": state,
        "message": doc["message"] if doc else None,
        "camera_status": doc["camera_status"] if doc else None,
        "source": doc["source"] if doc else None,
        "session_id": sess["session_id"] if sess else None,
        "session_started_at": sess["started_at"] if sess else None,
        "recording": {"active": live and rec["active"], "file": rec["file"]},
        "classes": list(schema.CLASSES),
        "vehicle_classes": list(schema.VEHICLE_CLASSES),
        "visible": visible,
        "unique": unique,
        "crossings": crossings,
        "line_totals": sess["line_totals"] if sess else {},
        "totals": _totals(visible, unique, crossings),
        "performance": {**perf, "frames_processed": sess["frames_processed"] if sess else 0},
        "device": doc["device"] if doc else None,
        "last_error": doc["last_error"] if doc else None,
    }


def session_view(s, live_instances, detail=False):
    """A stored session. `live` is true only while its desktop process is still reporting."""
    out = s.to_dict(detail=detail)
    out["live"] = not s.finalized and s.instance_id is not None and live_instances.get(s.device_id) == s.instance_id
    unique, crossings = out["unique"] or schema.zero_counts(), out["crossings"] or schema.zero_counts()
    out["totals"] = {k: v for k, v in _totals(schema.zero_counts(), unique, crossings).items()
                     if not k.startswith("visible")}
    return out
