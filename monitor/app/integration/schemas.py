"""Versioned data-interchange contract for future ATCS integration.

See docs/integration_contract.md. Nothing here opens a network connection;
these are plain builders/validators for JSON documents.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping, Optional

from app.detection.classes import MONITORED_CLASSES

SCHEMA_VERSION = "1.0"
SOURCES = ("camera", "video_file")
DIRECTIONS = ("a_to_b", "b_to_a")


class ContractError(ValueError):
    pass


def _counts(d: Optional[Mapping[str, Any]], name: str) -> dict[str, int]:
    if not isinstance(d, Mapping):
        raise ContractError(f"'{name}' must be an object")
    extra = set(d) - set(MONITORED_CLASSES)
    if extra:
        raise ContractError(f"'{name}' has unknown classes: {sorted(extra)}")
    out = {}
    for c in MONITORED_CLASSES:
        v = d.get(c, None)
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ContractError(f"'{name}.{c}' must be a non-negative integer")
        out[c] = v
    return out


def _timestamp(v: Any, name: str) -> str:
    if not isinstance(v, str):
        raise ContractError(f"'{name}' must be an ISO 8601 string")
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"'{name}' is not ISO 8601: {v}") from exc
    if dt.tzinfo is None:
        raise ContractError(f"'{name}' must include a time zone offset")
    return v


def _source(v: Any) -> str:
    if v not in SOURCES:
        raise ContractError(f"'source' must be one of {SOURCES}")
    return v


# ---------------------------------------------------------------- snapshot
def build_snapshot(session_id: str, timestamp: str, source: str, visible: Mapping[str, int],
                   crossings: Mapping[str, int]) -> dict:
    doc = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "timestamp": timestamp,
        "source": source,
        "visible": {c: int(visible.get(c, 0)) for c in MONITORED_CLASSES},
        "crossings": {c: int(crossings.get(c, 0)) for c in MONITORED_CLASSES},
    }
    validate_snapshot(doc)
    return doc


def validate_snapshot(doc: Any) -> dict:
    if not isinstance(doc, Mapping):
        raise ContractError("Snapshot must be a JSON object")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"Unsupported schema_version {doc.get('schema_version')!r}")
    if not isinstance(doc.get("session_id"), str) or not doc["session_id"]:
        raise ContractError("'session_id' must be a non-empty string")
    _timestamp(doc.get("timestamp"), "timestamp")
    _source(doc.get("source"))
    _counts(doc.get("visible"), "visible")
    _counts(doc.get("crossings"), "crossings")
    return dict(doc)


# ----------------------------------------------------------- session export
def session_summary(session: Mapping[str, Any], crossing_events: list[Mapping[str, Any]]) -> dict:
    """Session export document (also the JSON export format)."""
    stats = session.get("stats") or {}
    doc = {
        "schema_version": SCHEMA_VERSION,
        "kind": "session_summary",
        "session_id": session["session_id"],
        "source": session["source_type"],
        "source_id": session["source_id"],
        "started_at": session["started_at"],
        "ended_at": session.get("ended_at"),
        "status": session["status"],
        "model": {"name": session.get("model_name"), "version": session.get("model_version"),
                  "device": session.get("device")},
        "duration_s": stats.get("duration_s", 0.0),
        "frames_processed": stats.get("frames_processed", 0),
        "avg_inference_fps": stats.get("avg_inference_fps"),
        "unique": {c: int((stats.get("unique") or {}).get(c, 0)) for c in MONITORED_CLASSES},
        "crossings": {c: int((stats.get("crossings") or {}).get(c, 0)) for c in MONITORED_CLASSES},
        "line_totals": stats.get("line_totals") or {},
        "recordings": [
            {"file": r["path"].replace("\\", "/").rsplit("/", 1)[-1], "ok": bool(r["ok"]),
             "duration_s": r.get("duration_s"), "codec": r.get("codec")}
            for r in session.get("recordings", [])
        ],
        "crossing_events": [
            {"timestamp": e["ts"], "line_id": e["line_id"], "track_id": int(e["track_id"]),
             "class": e["cls"], "direction": e["direction"]}
            for e in crossing_events
        ],
    }
    validate_session_summary(doc)
    return doc


def validate_session_summary(doc: Any) -> dict:
    if not isinstance(doc, Mapping) or doc.get("kind") != "session_summary":
        raise ContractError("Not a session_summary document")
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ContractError(f"Unsupported schema_version {doc.get('schema_version')!r}")
    if not isinstance(doc.get("session_id"), str) or not doc["session_id"]:
        raise ContractError("'session_id' must be a non-empty string")
    _source(doc.get("source"))
    _timestamp(doc.get("started_at"), "started_at")
    if doc.get("ended_at") is not None:
        _timestamp(doc["ended_at"], "ended_at")
    _counts(doc.get("unique"), "unique")
    crossings = _counts(doc.get("crossings"), "crossings")
    events = doc.get("crossing_events")
    if not isinstance(events, list):
        raise ContractError("'crossing_events' must be a list")
    for i, e in enumerate(events):
        _timestamp(e.get("timestamp"), f"crossing_events[{i}].timestamp")
        if e.get("class") not in MONITORED_CLASSES:
            raise ContractError(f"crossing_events[{i}].class is invalid")
        if e.get("direction") not in DIRECTIONS:
            raise ContractError(f"crossing_events[{i}].direction is invalid")
        if isinstance(e.get("track_id"), bool) or not isinstance(e.get("track_id"), int):
            raise ContractError(f"crossing_events[{i}].track_id must be an integer")
    # Totals and events come from the same transaction and must agree.
    by_class = {c: 0 for c in MONITORED_CLASSES}
    for e in events:
        by_class[e["class"]] += 1
    if doc.get("status") in ("completed", "interrupted", "error") and by_class != crossings:
        raise ContractError("crossing totals do not match crossing_events")
    return dict(doc)


def to_json(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=False, ensure_ascii=False)


def snapshot_from_json(text: str) -> dict:
    try:
        return validate_snapshot(json.loads(text))
    except json.JSONDecodeError as exc:
        raise ContractError(f"Invalid JSON: {exc}") from exc
