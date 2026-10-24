"""Explicit schemas for the documents an ATCS Monitor desktop sends to the backend.

Standard library only, like monitor_client: the backend never imports the
monitor's code. The class names, sources, directions and summary fields mirror
monitor/app/integration/schemas.py (schema 1.0); desktop_status and
crossing_events are the documents added for the desktop link
(monitor/docs/desktop_telemetry.md).

Every validator returns a new, normalized dict and raises SchemaError on
anything unexpected, including unknown fields. Names that the desktop reports
(cameras, video and recording files, counting lines) must be bare names: a
value that looks like a filesystem path is rejected, never trimmed. Free text
(state messages, errors) is display-only; it is cleaned and any path in it is
reduced to its last component.
"""

import json
import math
import re
import unicodedata
from datetime import datetime

SCHEMA_VERSION = "1.0"
CLASSES = ("person", "car", "motorcycle", "bicycle", "bus", "truck")
VEHICLE_CLASSES = ("car", "motorcycle", "bus", "truck")
PEDESTRIAN_CLASSES = ("person",)
SOURCES = ("camera", "video_file")
DIRECTIONS = ("a_to_b", "b_to_a")
STATES = ("Idle", "Initializing", "Detecting", "Recording", "Stopping", "Error")
CAMERA_STATUSES = ("ok", "reconnecting", "error", "none")
FINAL_STATUSES = ("completed", "interrupted", "error")

MAX_COUNT = 10_000_000
MAX_VISIBLE = 10_000
MAX_LINES = 16
MAX_EVENTS_PER_PAGE = 500
MAX_RECORDINGS = 100
MAX_NAME = 128
MAX_LINE_ID = 64
MAX_TEXT = 300
MAX_SEQ = 2**53 - 1
MAX_TRACK_ID = 2**63 - 1
MAX_FPS = 1000.0
MAX_LATENCY_MS = 600_000.0
MAX_DURATION_S = 31 * 24 * 3600.0

HEX32_RE = re.compile(r"[0-9a-f]{32}")
DEVICE_RE = re.compile(r"[A-Za-z0-9:_.\-]{1,32}")
# Paths inside free text: C:\x\y, C:/x, \\server\share, /x/y
PATH_IN_TEXT_RE = re.compile(r"""[A-Za-z]:[\\/][^\s"'<>|]*|\\\\[^\s"'<>|]+|(?<![\w.])/(?:[^\s/"'<>|]+/)+[^\s/"'<>|]*""")


class SchemaError(ValueError):
    pass


# ------------------------------------------------------------------ JSON
def _no_constants(name):
    raise SchemaError(f"invalid number {name}")


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise SchemaError(f"duplicate field {key[:40]!r}")
        out[key] = value
    return out


def loads(raw):
    """Strict JSON: UTF-8, no NaN/Infinity, no duplicate keys, top level must be an object."""
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_no_constants, object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise SchemaError("body is not valid JSON") from exc
    if not isinstance(doc, dict):
        raise SchemaError("body must be a JSON object")
    return doc


# ------------------------------------------------------------- primitives
def _fields(doc, name, required, optional=()):
    if not isinstance(doc, dict):
        raise SchemaError(f"'{name}' must be an object")
    unknown = set(doc) - set(required) - set(optional)
    if unknown:
        raise SchemaError(f"'{name}' has unknown field(s): " + ", ".join(sorted(str(k)[:40] for k in unknown))[:200])
    missing = [k for k in required if k not in doc]
    if missing:
        raise SchemaError(f"'{name}' is missing field(s): " + ", ".join(missing))


def _int(value, name, lo, hi):
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise SchemaError(f"'{name}' must be an integer between {lo} and {hi}")
    return value


def _number(value, name, lo, hi, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise SchemaError(f"'{name}' must be a number between {lo} and {hi}")
    return float(value)


def _bool(value, name):
    if not isinstance(value, bool):
        raise SchemaError(f"'{name}' must be true or false")
    return value


def _choice(value, name, choices):
    if value not in choices:
        raise SchemaError(f"'{name}' must be one of: " + ", ".join(choices))
    return value


def _hex32(value, name):
    if not isinstance(value, str) or not HEX32_RE.fullmatch(value):
        raise SchemaError(f"'{name}' must be 32 lowercase hex characters")
    return value


def timestamp(value, name):
    """(original string, aware datetime). ISO 8601 with a time-zone offset."""
    if not isinstance(value, str) or len(value) > 40:
        raise SchemaError(f"'{name}' must be an ISO 8601 string")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SchemaError(f"'{name}' is not ISO 8601") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise SchemaError(f"'{name}' must include a time zone offset")
    return value, dt


def _has_control(text):
    return any(unicodedata.category(ch).startswith("C") for ch in text)


def safe_name(value, name, max_len=MAX_NAME, nullable=False):
    """A display name or bare file name. Paths are rejected, not shortened."""
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= max_len or value != value.strip():
        raise SchemaError(f"'{name}' must be a name of 1 to {max_len} characters")
    if _has_control(value) or any(ch in value for ch in "/\\:<>|\"?*") or ".." in value or value in (".",):
        raise SchemaError(f"'{name}' must be a plain name, not a path")
    return value


def clean_text(value, name, nullable=True):
    """Display-only free text: control characters removed, paths reduced to their last part."""
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise SchemaError(f"'{name}' must be a string")
    if len(value) > 4 * MAX_TEXT:
        raise SchemaError(f"'{name}' is too long")
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in value)
    text = PATH_IN_TEXT_RE.sub(lambda m: re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1] or "", text)
    return " ".join(text.split())[:MAX_TEXT]


def counts(value, name, hi=MAX_COUNT):
    """All six classes, non-negative integers, in contract order."""
    _fields(value, name, CLASSES)
    return {c: _int(value[c], f"{name}.{c}", 0, hi) for c in CLASSES}


def line_totals(value, name="line_totals"):
    if not isinstance(value, dict):
        raise SchemaError(f"'{name}' must be an object")
    if len(value) > MAX_LINES:
        raise SchemaError(f"'{name}' has more than {MAX_LINES} lines")
    out = {}
    for line_id, per_dir in value.items():
        safe_name(line_id, f"{name} line id", MAX_LINE_ID)
        _fields(per_dir, f"{name}.{line_id}", DIRECTIONS)
        out[line_id] = {d: counts(per_dir[d], f"{name}.{line_id}.{d}") for d in DIRECTIONS}
    return out


def zero_counts():
    return {c: 0 for c in CLASSES}


# --------------------------------------------------------- desktop_status
STATUS_FIELDS = ("schema_version", "kind", "instance_id", "instance_started_at", "seq", "sent_at", "app", "state",
                 "message", "camera_status", "source", "recording", "session", "performance", "device", "last_error")
SESSION_FIELDS = ("session_id", "started_at", "timestamp", "visible", "unique", "crossings", "line_totals",
                  "frames_processed")
PERFORMANCE_FIELDS = ("capture_fps", "inference_fps", "recording_fps", "latency_ms", "frames_dropped",
                      "recording_frames_dropped", "active_tracks")


def _version(doc, kind):
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SchemaError(f"unsupported schema_version (expected {SCHEMA_VERSION!r})")
    if doc.get("kind") != kind:
        raise SchemaError(f"'kind' must be {kind!r}")


def validate_status(doc):
    """desktop_status heartbeat. Returns (normalized doc, {name: datetime} of its timestamps)."""
    _fields(doc, "status", STATUS_FIELDS)
    _version(doc, "desktop_status")
    sent_at, sent_dt = timestamp(doc["sent_at"], "sent_at")
    inst_started, inst_dt = timestamp(doc["instance_started_at"], "instance_started_at")
    if sent_dt < inst_dt:
        raise SchemaError("'sent_at' is before 'instance_started_at'")
    times = {"sent_at": sent_dt, "instance_started_at": inst_dt}
    _fields(doc["app"], "app", ("name", "version"))
    out = {
        "schema_version": SCHEMA_VERSION,
        "kind": "desktop_status",
        "instance_id": _hex32(doc["instance_id"], "instance_id"),
        "instance_started_at": inst_started,
        "seq": _int(doc["seq"], "seq", 0, MAX_SEQ),
        "sent_at": sent_at,
        "app": {"name": safe_name(doc["app"]["name"], "app.name", 64),
                "version": safe_name(doc["app"]["version"], "app.version", 32)},
        "state": _choice(doc["state"], "state", STATES),
        "message": clean_text(doc["message"], "message"),
        "camera_status": _choice(doc["camera_status"], "camera_status", CAMERA_STATUSES),
        "device": None,
        "last_error": clean_text(doc["last_error"], "last_error"),
    }
    if doc["device"] is not None:
        if not isinstance(doc["device"], str) or not DEVICE_RE.fullmatch(doc["device"]):
            raise SchemaError("'device' must be an inference device name such as 'cpu' or 'cuda:0'")
        out["device"] = doc["device"]

    src = doc["source"]
    if src is None:
        out["source"] = None
    else:
        _fields(src, "source", ("kind", "label"))
        out["source"] = {"kind": _choice(src["kind"], "source.kind", SOURCES), "label": safe_name(src["label"], "source.label")}

    rec = doc["recording"]
    _fields(rec, "recording", ("active", "file"))
    out["recording"] = {"active": _bool(rec["active"], "recording.active"),
                        "file": safe_name(rec["file"], "recording.file", nullable=True)}

    perf = doc["performance"]
    _fields(perf, "performance", PERFORMANCE_FIELDS)
    out["performance"] = {
        "capture_fps": _number(perf["capture_fps"], "performance.capture_fps", 0, MAX_FPS),
        "inference_fps": _number(perf["inference_fps"], "performance.inference_fps", 0, MAX_FPS),
        "recording_fps": _number(perf["recording_fps"], "performance.recording_fps", 0, MAX_FPS),
        "latency_ms": _number(perf["latency_ms"], "performance.latency_ms", 0, MAX_LATENCY_MS),
        # Measurements, not counters: the capture buffer and its drop count are
        # recreated when a camera reconnects, so these may go down.
        "frames_dropped": _int(perf["frames_dropped"], "performance.frames_dropped", 0, MAX_SEQ),
        "recording_frames_dropped": _int(perf["recording_frames_dropped"], "performance.recording_frames_dropped", 0, MAX_SEQ),
        "active_tracks": _int(perf["active_tracks"], "performance.active_tracks", 0, MAX_VISIBLE),
    }

    sess = doc["session"]
    if sess is None:
        out["session"] = None
    else:
        if out["source"] is None:
            raise SchemaError("'source' is required while a session is reported")
        _fields(sess, "session", SESSION_FIELDS)
        started, started_dt = timestamp(sess["started_at"], "session.started_at")
        ts, ts_dt = timestamp(sess["timestamp"], "session.timestamp")
        if ts_dt < started_dt:
            raise SchemaError("'session.timestamp' is before 'session.started_at'")
        out["session"] = {
            "session_id": _hex32(sess["session_id"], "session.session_id"),
            "started_at": started,
            "timestamp": ts,
            # instantaneous: objects in the current frame
            "visible": counts(sess["visible"], "session.visible", MAX_VISIBLE),
            # cumulative for the whole session
            "unique": counts(sess["unique"], "session.unique"),
            "crossings": counts(sess["crossings"], "session.crossings"),
            "line_totals": line_totals(sess["line_totals"], "session.line_totals"),
            "frames_processed": _int(sess["frames_processed"], "session.frames_processed", 0, MAX_SEQ),
        }
        times.update({"session.started_at": started_dt, "session.timestamp": ts_dt})
    return out, times


# -------------------------------------------------------- crossing_events
EVENTS_FIELDS = ("schema_version", "kind", "session_id", "started_at", "source", "source_id", "events")
EVENT_FIELDS = ("timestamp", "line_id", "track_id", "class", "direction")


def validate_events(doc):
    """One page of crossing events. Returns (normalized doc, started_at datetime, [event datetimes])."""
    _fields(doc, "events document", EVENTS_FIELDS)
    _version(doc, "crossing_events")
    started, started_dt = timestamp(doc["started_at"], "started_at")
    events = doc["events"]
    if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS_PER_PAGE:
        raise SchemaError(f"'events' must be a list of 1 to {MAX_EVENTS_PER_PAGE} events")
    out_events, times, keys = [], [], set()
    for i, e in enumerate(events):
        name = f"events[{i}]"
        _fields(e, name, EVENT_FIELDS)
        ts, ts_dt = timestamp(e["timestamp"], f"{name}.timestamp")
        ev = {
            "timestamp": ts,
            "line_id": safe_name(e["line_id"], f"{name}.line_id", MAX_LINE_ID),
            "track_id": _int(e["track_id"], f"{name}.track_id", 0, MAX_TRACK_ID),
            "class": _choice(e["class"], f"{name}.class", CLASSES),
            "direction": _choice(e["direction"], f"{name}.direction", DIRECTIONS),
        }
        key = (ev["line_id"], ev["track_id"], ev["direction"])
        if key in keys:
            raise SchemaError(f"{name} repeats an event of this page")
        keys.add(key)
        out_events.append(ev)
        times.append(ts_dt)
    out = {
        "schema_version": SCHEMA_VERSION,
        "kind": "crossing_events",
        "session_id": _hex32(doc["session_id"], "session_id"),
        "started_at": started,
        "source": _choice(doc["source"], "source", SOURCES),
        "source_id": safe_name(doc["source_id"], "source_id"),
        "events": out_events,
    }
    return out, started_dt, times


# -------------------------------------------------------- session_summary
SUMMARY_FIELDS = ("schema_version", "kind", "session_id", "source", "source_id", "started_at", "ended_at", "status",
                  "model", "duration_s", "frames_processed", "avg_inference_fps", "unique", "crossings",
                  "line_totals", "recordings", "crossing_event_count", "stats_complete")
RECORDING_FIELDS = ("file", "ok", "duration_s", "codec")


def validate_summary(doc):
    """session_summary without crossing_events (they are uploaded in pages), plus
    crossing_event_count and stats_complete. Returns (normalized doc, started, ended datetimes)."""
    _fields(doc, "summary", SUMMARY_FIELDS)
    _version(doc, "session_summary")
    started, started_dt = timestamp(doc["started_at"], "started_at")
    ended, ended_dt = timestamp(doc["ended_at"], "ended_at")
    if ended_dt < started_dt:
        raise SchemaError("'ended_at' is before 'started_at'")
    status = _choice(doc["status"], "status", FINAL_STATUSES)
    stats_complete = _bool(doc["stats_complete"], "stats_complete")
    if not stats_complete and status == "completed":
        raise SchemaError("a completed session must have complete statistics")

    model = doc["model"]
    _fields(model, "model", ("name", "version", "device"))
    device = model["device"]
    if device is not None and (not isinstance(device, str) or not DEVICE_RE.fullmatch(device)):
        raise SchemaError("'model.device' must be an inference device name")
    version = model["version"]
    if version is not None and (not isinstance(version, str) or len(version) > MAX_NAME or _has_control(version)
                                or any(ch in version for ch in "/\\")):
        raise SchemaError("'model.version' must be a short description without paths")

    recordings = doc["recordings"]
    if not isinstance(recordings, list) or len(recordings) > MAX_RECORDINGS:
        raise SchemaError(f"'recordings' must be a list of at most {MAX_RECORDINGS} entries")
    out_recs = []
    for i, r in enumerate(recordings):
        _fields(r, f"recordings[{i}]", RECORDING_FIELDS)
        codec = r["codec"]
        if codec is not None and (not isinstance(codec, str) or not re.fullmatch(r"[A-Za-z0-9_.\-]{1,16}", codec)):
            raise SchemaError(f"'recordings[{i}].codec' is invalid")
        out_recs.append({
            "file": safe_name(r["file"], f"recordings[{i}].file"),
            "ok": _bool(r["ok"], f"recordings[{i}].ok"),
            "duration_s": _number(r["duration_s"], f"recordings[{i}].duration_s", 0, MAX_DURATION_S, nullable=True),
            "codec": codec,
        })

    out = {
        "schema_version": SCHEMA_VERSION,
        "kind": "session_summary",
        "session_id": _hex32(doc["session_id"], "session_id"),
        "source": _choice(doc["source"], "source", SOURCES),
        "source_id": safe_name(doc["source_id"], "source_id"),
        "started_at": started,
        "ended_at": ended,
        "status": status,
        "model": {"name": safe_name(model["name"], "model.name", nullable=True), "version": version, "device": device},
        "duration_s": _number(doc["duration_s"], "duration_s", 0, MAX_DURATION_S),
        "frames_processed": _int(doc["frames_processed"], "frames_processed", 0, MAX_SEQ),
        "avg_inference_fps": _number(doc["avg_inference_fps"], "avg_inference_fps", 0, MAX_FPS, nullable=True),
        "unique": counts(doc["unique"], "unique"),
        "crossings": counts(doc["crossings"], "crossings"),
        "line_totals": line_totals(doc["line_totals"]),
        "recordings": out_recs,
        "crossing_event_count": _int(doc["crossing_event_count"], "crossing_event_count", 0, MAX_COUNT),
        "stats_complete": stats_complete,
    }
    if sum(out["crossings"].values()) != out["crossing_event_count"]:
        raise SchemaError("'crossings' totals do not add up to 'crossing_event_count'")
    return out, started_dt, ended_dt


def canonical(doc):
    """Stable text of a normalized document, used to compare summaries."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
