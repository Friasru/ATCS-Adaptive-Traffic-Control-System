"""Shared helpers for the desktop link tests.

backend_schema() loads the backend's own validator (backend/app/services/
desktop_schema.py, standard library only) straight from this repository, so
the documents built by the desktop are checked against exactly the rules the
backend applies.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

from app.desktop_link.client import Response
from app.desktop_link.config import LinkConfig, Secret
from app.storage.repository import iso

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
TOKEN = "atcsdev_" + "T" * 43
_schema = None


def backend_schema():
    global _schema
    if _schema is None:
        path = BACKEND / "app" / "services" / "desktop_schema.py"
        if not path.is_file():
            pytest.skip("backend/app/services/desktop_schema.py not found")
        spec = importlib.util.spec_from_file_location("atcs_backend_desktop_schema", path)
        _schema = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_schema)
    return _schema


def link_config(url="http://127.0.0.1:5000", token=TOKEN, name="test-desk"):
    return LinkConfig(backend_url=url, device_name=name, token=Secret(token))


class FakeClient:
    """Scripted backend. Every document is validated with the backend's schema."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.script: dict[str, list] = {"status": [], "events": [], "summary": []}
        self.events: dict[str, dict] = {}  # session -> {key: event}
        self.summaries: dict[str, dict] = {}

    def respond(self, kind, *responses):
        self.script[kind].extend(responses)

    def _next(self, kind):
        if self.script[kind]:
            r = self.script[kind].pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return None

    def post_status(self, doc):
        backend_schema().validate_status(doc)
        self.calls.append(("status", "", doc))
        return self._next("status") or Response(200, {"accepted": True})

    def post_events(self, sid, doc):
        out, _, _ = backend_schema().validate_events(doc)
        assert out["session_id"] == sid
        self.calls.append(("events", sid, doc))
        r = self._next("events")
        if r is not None:
            return r
        store = self.events.setdefault(sid, {})
        new = 0
        for e in out["events"]:
            key = (e["line_id"], e["track_id"], e["direction"])
            new += key not in store
            store.setdefault(key, e)
        return Response(200, {"inserted": new, "duplicates": len(out["events"]) - new, "stored_events": len(store)})

    def post_summary(self, sid, doc):
        out, _, _ = backend_schema().validate_summary(doc)
        self.calls.append(("summary", sid, doc))
        r = self._next("summary")
        if r is not None:
            return r
        stored = len(self.events.get(sid, {}))
        if stored != out["crossing_event_count"]:
            return Response(409, {"code": "events_incomplete", "error": "upload events", "stored_events": stored})
        if sid in self.summaries:
            if self.summaries[sid] != out:
                return Response(409, {"code": "summary_conflict", "error": "different summary"})
            return Response(200, {"finalized": True, "duplicate": True})
        self.summaries[sid] = out
        return Response(201, {"finalized": True, "duplicate": False, "status": out["status"]})

    def kinds(self):
        return [k for k, _, _ in self.calls]


def local_session(repo, *, status="completed", events=(), source=("camera", "0:Integrated Camera"), stats=True,
                  recordings=(), crossings=None, started_at=None):
    """Create a finished (or running) session in the app's own history database."""
    sid = uuid.uuid4().hex
    repo.create_session(sid, source[0], source[1], "yolo26n.pt", "yolo26n.pt sha256:abc ultralytics 8.4.184", "cpu",
                        {}, started_at=started_at or iso())
    rows = [(sid, iso(), "line-1", track, cls, direction) for track, cls, direction in events]
    repo.insert_events(rows, [])
    for name, ok in recordings:
        rec = repo.add_recording(sid, f"/home/user/recordings/{name}", "avc1", "mp4", iso())
        if ok is not None:
            repo.finish_recording(rec, {"ok": ok, "duration_s": 2.5, "frames_written": 25})
    if status == "running":
        return sid
    tally = {c: 0 for c in ("person", "car", "motorcycle", "bicycle", "bus", "truck")}
    for _t, cls, _d in events:
        tally[cls] += 1
    if stats:
        lt = {"line-1": {"a_to_b": dict(tally), "b_to_a": {c: 0 for c in tally}}}
        repo.finish_session(sid, status, {"unique": dict(tally), "crossings": crossings or dict(tally),
                                          "line_totals": lt, "duration_s": 12.5, "frames_processed": 250,
                                          "avg_inference_fps": 20.0})
    else:
        with repo.transaction() as c:
            c.execute("UPDATE sessions SET status=?, ended_at=? WHERE session_id=?", (status, iso(), sid))
    return sid
