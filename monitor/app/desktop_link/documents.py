"""Telemetry documents (schema 1.0, docs/desktop_telemetry.md section 3).

The backend validates these with backend/app/services/desktop_schema.py; the
tests check every builder here against that validator. Values come from the
engine's snapshot (live status) and from the app's own SQLite history
(finished sessions), which stays the source of truth: nothing is invented.

Names the backend must receive as bare names (camera labels, video and
recording files, counting lines) are reduced here with bare_name(). Local
camera sources are stored as "<index>:<name>", which is not a bare name, so the
same mapping is used for the live status and the summary of a session.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.detection.classes import MONITORED_CLASSES, zero_counts

SCHEMA_VERSION = "1.0"
APP_NAME = "ATCS Monitor"
STATES = ("Idle", "Initializing", "Detecting", "Recording", "Stopping", "Error")
RUNNING_STATES = ("Initializing", "Detecting", "Recording", "Stopping")
FINAL_STATUSES = ("completed", "interrupted", "error")
DIRECTIONS = ("a_to_b", "b_to_a")
MAX_EVENTS_PER_PAGE = 500
# The events endpoint accepts at most 128 KB; leave room for the envelope.
MAX_PAGE_BYTES = 120 * 1024
MAX_FPS = 1000.0
MAX_LATENCY_MS = 600_000.0
MAX_VISIBLE = 10_000
MAX_NAME = 128
MAX_TEXT = 300
DEVICE_RE = re.compile(r"[A-Za-z0-9:_.\-]{1,32}")
CODEC_RE = re.compile(r"[A-Za-z0-9_.\-]{1,16}")
FORBIDDEN = set('/\\:<>|"?*')
PATH_IN_TEXT_RE = re.compile(r"""[A-Za-z]:[\\/][^\s"'<>|]*|\\\\[^\s"'<>|]+|(?<![\w.])/(?:[^\s/"'<>|]+/)+[^\s/"'<>|]*""")


def bare_name(text: object, fallback: str, max_len: int = MAX_NAME) -> str:
    """A name the backend accepts: no path characters, no '..', no control characters."""
    s = "".join("_" if ch in FORBIDDEN or unicodedata.category(ch).startswith("C") else ch for ch in str(text or ""))
    while ".." in s:
        s = s.replace("..", "._")
    s = s.strip()[:max_len].strip()
    return s if s and s != "." else fallback


def file_name(path: object) -> Optional[str]:
    """Last component of a local path (recordings, video files), as a bare name."""
    if not path:
        return None
    return bare_name(re.split(r"[\\/]", str(path).rstrip("\\/"))[-1], "file")


def source_name(source_type: str, source_id: str) -> str:
    """Backend source label for a local session source.

    Cameras are stored locally as "<index>:<device name>"; the device name is
    used, or "Camera <index>" without one. Video files are stored by file name.
    """
    if source_type == "camera":
        index, sep, name = str(source_id).partition(":")
        if not sep:
            index, name = "", index
        return bare_name(name, f"Camera {bare_name(index, '0', 8)}")
    return bare_name(file_name(source_id) or "", "video")


def clean_text(text: object) -> Optional[str]:
    """Display text without local paths (only the last component is kept)."""
    if text is None:
        return None
    s = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in str(text))
    s = PATH_IN_TEXT_RE.sub(lambda m: re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1] or "", s)
    return " ".join(s.split())[:MAX_TEXT]


def _counts(d: Optional[dict], hi: int = 10_000_000) -> dict:
    d = d or {}
    return {c: max(0, min(hi, int(d.get(c, 0) or 0))) for c in MONITORED_CLASSES}


def _clamp(v, hi) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if f != f or f < 0 else min(f, hi)


def line_totals(raw: Optional[dict]) -> dict:
    out = {}
    for line_id, per_dir in (raw or {}).items():
        out[bare_name(line_id, "line", 64)] = {d: _counts((per_dir or {}).get(d)) for d in DIRECTIONS}
    return out


# ------------------------------------------------------------- live status
@dataclass(frozen=True)
class LiveState:
    """What the window shows, captured on the GUI thread (plain values only)."""

    state: str = "Idle"
    message: str = ""
    last_error: Optional[str] = None
    session_id: Optional[str] = None  # set only while that session is running
    timestamp: str = ""  # when the counts were read (ISO 8601)
    recording_active: bool = False
    recording_file: Optional[str] = None
    camera_status: str = "none"
    visible: dict = field(default_factory=zero_counts)
    unique: dict = field(default_factory=zero_counts)
    crossings: dict = field(default_factory=zero_counts)
    line_totals: dict = field(default_factory=dict)
    frames_processed: int = 0
    capture_fps: float = 0.0
    inference_fps: float = 0.0
    recording_fps: float = 0.0
    latency_ms: float = 0.0
    frames_dropped: int = 0
    recording_frames_dropped: int = 0
    active_tracks: int = 0
    device: Optional[str] = None

    @property
    def running(self) -> bool:
        return self.state in RUNNING_STATES


@dataclass(frozen=True)
class SessionInfo:
    """Fixed facts about a session, read once from the local history."""

    session_id: str
    started_at: str
    source_kind: str
    source_label: str


def session_info(session: dict) -> SessionInfo:
    return SessionInfo(session["session_id"], session["started_at"], session["source_type"],
                       source_name(session["source_type"], session["source_id"]))


def build_status(live: LiveState, session: Optional[SessionInfo], *, instance_id: str, instance_started_at: str,
                 seq: int, sent_at: str, app_version: str) -> dict:
    sess = None
    source = None
    if session is not None and live.session_id == session.session_id:
        source = {"kind": session.source_kind, "label": session.source_label}
        sess = {
            "session_id": session.session_id,
            "started_at": session.started_at,
            # never before the session start, even if the clocks of two reads differ slightly
            "timestamp": max(live.timestamp or session.started_at, session.started_at),
            "visible": _counts(live.visible, MAX_VISIBLE),
            "unique": _counts(live.unique),
            "crossings": _counts(live.crossings),
            "line_totals": line_totals(live.line_totals),
            "frames_processed": max(0, int(live.frames_processed)),
        }
    device = live.device if isinstance(live.device, str) and DEVICE_RE.fullmatch(live.device) else None
    active = live.running and sess is not None
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "desktop_status",
        "instance_id": instance_id,
        "instance_started_at": instance_started_at,
        "seq": seq,
        "sent_at": sent_at,
        "app": {"name": APP_NAME, "version": bare_name(app_version, "unknown", 32)},
        "state": live.state if live.state in STATES else "Error",
        "message": clean_text(live.message),
        "camera_status": live.camera_status if active or live.camera_status in ("error", "none") else "none",
        "source": source,
        "recording": {"active": bool(live.recording_active and active),
                      "file": file_name(live.recording_file) if active else None},
        "session": sess,
        "performance": {
            "capture_fps": _clamp(live.capture_fps, MAX_FPS) if active else 0.0,
            "inference_fps": _clamp(live.inference_fps, MAX_FPS) if active else 0.0,
            "recording_fps": _clamp(live.recording_fps, MAX_FPS) if active else 0.0,
            "latency_ms": _clamp(live.latency_ms, MAX_LATENCY_MS) if active else 0.0,
            "frames_dropped": max(0, int(live.frames_dropped)) if active else 0,
            "recording_frames_dropped": max(0, int(live.recording_frames_dropped)) if active else 0,
            "active_tracks": max(0, min(MAX_VISIBLE, int(live.active_tracks))) if active else 0,
        },
        "device": device,
        "last_error": clean_text(live.last_error),
    }


# --------------------------------------------------------- finished session
@dataclass
class FrozenSession:
    """A finished session ready for upload: event pages first, then the summary."""

    session_id: str
    envelope: dict  # crossing_events fields shared by every page
    events: list
    summary: dict
    notes: list = field(default_factory=list)  # what was adjusted, for the log


def _event(e: dict) -> dict:
    return {"timestamp": e["ts"], "line_id": bare_name(e["line_id"], "line", 64), "track_id": int(e["track_id"]),
            "class": e["cls"], "direction": e["direction"]}


def _tally(events: Iterable[dict]) -> tuple[dict, dict]:
    by_class, lines = zero_counts(), {}
    for e in events:
        by_class[e["class"]] += 1
        per_dir = lines.setdefault(e["line_id"], {d: zero_counts() for d in DIRECTIONS})
        per_dir[e["direction"]][e["class"]] += 1
    return by_class, lines


def build_session(session: dict, local_events: list[dict], now_iso: str) -> FrozenSession:
    """Freeze a finished local session (Repository.get_session + crossing_events).

    * Crossing totals always equal the events that are uploaded.
    * stats_complete is false when the app never saved the session's statistics
      (it crashed: no stats row) or when saved events are missing. Unknown
      values are then sent as zero / null and the backend keeps the counts it
      received live (counts_origin "last_live_update").
    """
    status = session["status"]
    if status not in FINAL_STATUSES:
        raise ValueError(f"session {session['session_id']} is not finished ({status})")
    notes = []
    events, seen = [], set()
    for raw in local_events:
        e = _event(raw)
        if e["class"] not in MONITORED_CLASSES or e["direction"] not in DIRECTIONS or e["track_id"] < 0:
            notes.append("skipped an invalid local event")
            continue
        key = (e["line_id"], e["track_id"], e["direction"])
        if key in seen:
            notes.append("skipped a repeated local event")
            continue
        seen.add(key)
        events.append(e)
    tally, event_lines = _tally(events)

    stats = session.get("stats")
    complete = stats is not None and _counts(stats.get("crossings")) == tally
    if stats is not None and not complete:
        notes.append("saved crossing totals differ from the saved events")
    if complete:
        unique = _counts(stats.get("unique"))
        lines = line_totals(stats.get("line_totals"))
        duration = _clamp(stats.get("duration_s"), 31 * 24 * 3600.0)
        frames = max(0, int(stats.get("frames_processed") or 0))
        fps = stats.get("avg_inference_fps")
        fps = None if fps is None else _clamp(fps, MAX_FPS)
    else:
        # Not invented: unique objects, frames and duration are unknown.
        unique, lines, duration, frames, fps = zero_counts(), event_lines, 0.0, 0, None
    if not complete and status == "completed":
        # A completed session must have complete statistics (schema rule); this
        # only happens if saved events are missing, which the app reports as an error.
        status = "error"
        notes.append("status changed from completed to error because events are missing")

    source = source_name(session["source_type"], session["source_id"])
    device = session.get("device")
    version = session.get("model_version")
    if version is not None:
        version = (clean_text(version) or "")[:MAX_NAME] or None
        if version and ("/" in version or "\\" in version):
            version = None
    recordings = []
    for r in (session.get("recordings") or [])[:100]:
        codec = r.get("codec")
        recordings.append({
            "file": file_name(r.get("path")) or "recording",
            "ok": bool(r.get("ok")),
            "duration_s": None if r.get("duration_s") is None else _clamp(r["duration_s"], 31 * 24 * 3600.0),
            "codec": codec if isinstance(codec, str) and CODEC_RE.fullmatch(codec) else None,
        })
    started = session["started_at"]
    ended = max(session.get("ended_at") or now_iso, started)
    summary = {
        "schema_version": SCHEMA_VERSION,
        "kind": "session_summary",
        "session_id": session["session_id"],
        "source": session["source_type"],
        "source_id": source,
        "started_at": started,
        "ended_at": ended,
        "status": status,
        "model": {
            "name": bare_name(session["model_name"], "model") if session.get("model_name") else None,
            "version": version,
            "device": device if isinstance(device, str) and DEVICE_RE.fullmatch(device) else None,
        },
        "duration_s": duration,
        "frames_processed": frames,
        "avg_inference_fps": fps,
        "unique": unique,
        "crossings": tally,
        "line_totals": lines,
        "recordings": recordings,
        "crossing_event_count": len(events),
        "stats_complete": complete,
    }
    envelope = {"schema_version": SCHEMA_VERSION, "kind": "crossing_events", "session_id": session["session_id"],
                "started_at": started, "source": session["source_type"], "source_id": source}
    return FrozenSession(session["session_id"], envelope, events, summary, sorted(set(notes)))


def event_pages(envelope: dict, events: list, start: int = 0) -> Iterable[tuple[int, dict]]:
    """(index after the page, page document) from event `start` on; each page at most
    500 events and MAX_PAGE_BYTES of JSON."""
    i = start
    base = len(json.dumps({**envelope, "events": []}))
    while i < len(events):
        size, j = base, i
        while j < len(events) and j - i < MAX_EVENTS_PER_PAGE:
            n = len(json.dumps(events[j], ensure_ascii=False)) + 2
            if j > i and size + n > MAX_PAGE_BYTES:
                break
            size += n
            j += 1
        yield j, {**envelope, "events": events[i:j]}
        i = j
