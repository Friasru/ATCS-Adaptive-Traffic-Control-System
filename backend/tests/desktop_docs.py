"""Builders for the documents a desktop ATCS Monitor sends (monitor/docs/desktop_telemetry.md).

The shapes follow monitor/app/integration/schemas.py; the backend tests never
import the monitor's code.
"""

import copy
import uuid
from datetime import datetime, timedelta, timezone

CLASSES = ("person", "car", "motorcycle", "bicycle", "bus", "truck")


def iso(dt=None, **delta):
    dt = dt or datetime.now(timezone.utc)
    return (dt + timedelta(**delta)).isoformat(timespec="milliseconds")


def counts(**values):
    return {c: values.get(c, 0) for c in CLASSES}


def hexid():
    return uuid.uuid4().hex


def lines(a_car=0, b_person=0, line_id="line-1"):
    return {line_id: {"a_to_b": counts(car=a_car), "b_to_a": counts(person=b_person)}}


def session_block(session_id, started_at, *, visible=None, unique=None, crossings=None, line_totals=None,
                  frames=10, timestamp=None):
    return {
        "session_id": session_id,
        "started_at": started_at,
        "timestamp": timestamp or iso(),
        "visible": visible or counts(car=1),
        "unique": unique or counts(car=1),
        "crossings": crossings or counts(),
        "line_totals": line_totals if line_totals is not None else lines(),
        "frames_processed": frames,
    }


def status(instance_id, seq, *, session=None, state=None, instance_started_at=None, sent_at=None, **overrides):
    doc = {
        "schema_version": "1.0",
        "kind": "desktop_status",
        "instance_id": instance_id,
        "instance_started_at": instance_started_at or iso(seconds=-60),
        "seq": seq,
        "sent_at": sent_at or iso(),
        "app": {"name": "ATCS Monitor", "version": "1.0.0"},
        "state": state or ("Detecting" if session else "Idle"),
        "message": "Detecting on camera:0 (cuda:0, 960 px)" if session else "Stopped",
        "camera_status": "ok" if session else "none",
        "source": {"kind": "camera", "label": "Integrated Camera"} if session else None,
        "recording": {"active": False, "file": None},
        "session": session,
        "performance": {"capture_fps": 29.8, "inference_fps": 10.4, "recording_fps": 0.0, "latency_ms": 11.2,
                        "frames_dropped": 0, "recording_frames_dropped": 0, "active_tracks": 3},
        "device": "cuda:0",
        "last_error": None,
    }
    doc.update(overrides)
    return doc


def event(track_id, *, cls="car", direction="a_to_b", line_id="line-1", timestamp=None):
    return {"timestamp": timestamp or iso(), "line_id": line_id, "track_id": track_id, "class": cls,
            "direction": direction}


def events(session_id, started_at, evs, source="camera", source_id="Integrated Camera"):
    return {"schema_version": "1.0", "kind": "crossing_events", "session_id": session_id, "started_at": started_at,
            "source": source, "source_id": source_id, "events": evs}


def summary(session_id, started_at, *, crossings=None, unique=None, line_totals=None, status="completed",
            event_count=None, stats_complete=True, frames=100, source="camera", source_id="Integrated Camera",
            recordings=None):
    crossings = crossings or counts()
    return {
        "schema_version": "1.0",
        "kind": "session_summary",
        "session_id": session_id,
        "source": source,
        "source_id": source_id,
        "started_at": started_at,
        "ended_at": iso(),
        "status": status,
        "model": {"name": "yolo26n.pt", "version": "yolo26n.pt sha256:9b09cc8bf347 ultralytics 8.4.184",
                  "device": "cuda:0"},
        "duration_s": 42.5,
        "frames_processed": frames,
        "avg_inference_fps": 10.2,
        "unique": unique or counts(car=1),
        "crossings": crossings,
        "line_totals": line_totals if line_totals is not None else lines(),
        "recordings": recordings if recordings is not None else
        [{"file": "atcs_20261023-120000_3f1c0d6e.mp4", "ok": True, "duration_s": 40.1, "codec": "avc1"}],
        "crossing_event_count": sum(crossings.values()) if event_count is None else event_count,
        "stats_complete": stats_complete,
    }


def mutate(doc, path, value):
    """Copy of doc with doc[path[0]][path[1]]... set to value (or removed for ...)."""
    out = copy.deepcopy(doc)
    target = out
    for key in path[:-1]:
        target = target[key]
    if value is ...:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return out
