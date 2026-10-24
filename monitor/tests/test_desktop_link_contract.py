"""Desktop link documents against the backend's Phase 5a validators (shared contract).

The backend validator module is loaded from backend/app/services/desktop_schema.py,
so a change on either side that breaks compatibility fails here.
"""

import json
from pathlib import Path

import pytest

from app.desktop_link import documents as d
from app.detection.classes import MONITORED_CLASSES
from app.storage.repository import Repository, iso
from tests.link_support import backend_schema, local_session

FIXTURE = Path(__file__).parent / "data" / "desktop_link_documents.json"
SID = "0123456789abcdef0123456789abcdef"
INST = "fedcba9876543210fedcba9876543210"
T0 = "2026-10-24T10:00:00.000+00:00"
T1 = "2026-10-24T10:00:05.000+00:00"


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "h.sqlite3")
    yield r
    r.close()


def status(live, info=None, seq=1):
    return d.build_status(live, info, instance_id=INST, instance_started_at=T0, seq=seq, sent_at=T1,
                          app_version="1.0.0")


def test_constants_match_backend():
    s = backend_schema()
    assert tuple(MONITORED_CLASSES) == s.CLASSES
    assert d.STATES == s.STATES
    assert d.FINAL_STATUSES == s.FINAL_STATUSES
    assert d.DIRECTIONS == s.DIRECTIONS
    assert d.MAX_EVENTS_PER_PAGE == s.MAX_EVENTS_PER_PAGE
    assert d.SCHEMA_VERSION == s.SCHEMA_VERSION


def live_detecting(**kw):
    base = dict(state="Detecting", message="Detecting on camera:0:Integrated Camera (cpu, 640 px)", session_id=SID,
                timestamp=T1, camera_status="ok", visible={"car": 2, "person": 1}, unique={"car": 3},
                crossings={"car": 1}, line_totals={"line-1": {"a_to_b": {"car": 1}, "b_to_a": {}}},
                frames_processed=55, capture_fps=29.9, inference_fps=14.8, latency_ms=21.0, active_tracks=3,
                device="cpu")
    base.update(kw)
    return d.LiveState(**base)


INFO = d.SessionInfo(SID, T0, "camera", "Integrated Camera")


@pytest.mark.parametrize("live,info", [
    (d.LiveState(), None),
    (live_detecting(), INFO),
    (live_detecting(state="Recording", recording_active=True, recording_file=r"C:\Users\a\recordings\r.mp4"), INFO),
    (live_detecting(state="Stopping"), INFO),
    (live_detecting(state="Detecting", camera_status="reconnecting"), INFO),
    (d.LiveState(state="Initializing", message="Opening file:clip.mp4"), None),
    (d.LiveState(state="Error", camera_status="error", last_error="Cannot open camera /dev/video0"), None),
    (live_detecting(capture_fps=5000.0, latency_ms=-3, active_tracks=99999, device="weird device!"), INFO),
])
def test_status_documents_validate(live, info):
    doc = status(live, info)
    out, _times = backend_schema().validate_status(doc)
    assert out == doc  # nothing needed normalising: the desktop sends canonical values
    if info is None:
        assert doc["session"] is None and doc["source"] is None


def test_status_session_values():
    doc = status(live_detecting(), INFO)
    sess = doc["session"]
    assert sess["session_id"] == SID and sess["started_at"] == T0
    assert sess["visible"]["car"] == 2 and sess["unique"]["car"] == 3 and sess["crossings"]["car"] == 1
    assert list(sess["visible"]) == list(MONITORED_CLASSES)
    assert sess["line_totals"]["line-1"]["a_to_b"]["car"] == 1
    assert sess["frames_processed"] == 55
    assert doc["performance"]["active_tracks"] == 3 and doc["performance"]["inference_fps"] == 14.8
    assert doc["source"] == {"kind": "camera", "label": "Integrated Camera"}


def test_status_without_matching_session_reports_none():
    other = d.SessionInfo("f" * 32, T0, "camera", "Integrated Camera")
    doc = status(live_detecting(), other)
    assert doc["session"] is None and doc["recording"]["active"] is False
    assert doc["performance"]["inference_fps"] == 0.0


def test_local_paths_are_not_sent():
    doc = status(live_detecting(state="Recording", recording_active=True,
                                recording_file="/home/alberto/secret/rec.mp4",
                                message=r"Recording to C:\Users\alberto\Videos\rec.mp4",
                                last_error="failed: /home/alberto/x/y.mp4"), INFO)
    text = json.dumps(doc)
    assert "alberto" not in text
    assert doc["recording"]["file"] == "rec.mp4"


@pytest.mark.parametrize("kind,local,label", [
    ("camera", "0:Integrated Camera", "Integrated Camera"),
    ("camera", "2:HD Pro Webcam C920 (046d:082d)", "HD Pro Webcam C920 (046d_082d)"),
    ("camera", "1:", "Camera 1"),
    ("camera", "3:../../etc", ".__.__etc"),
    ("video_file", "clip.mp4", "clip.mp4"),
    ("video_file", r"C:\Users\a\clip.mp4", "clip.mp4"),
    ("video_file", "a..b.mp4", "a._b.mp4"),
    ("video_file", "", "video"),
])
def test_source_names_are_bare_names(kind, local, label):
    name = d.source_name(kind, local)
    assert name == label
    backend_schema().safe_name(name, "source_id")


def test_completed_session_summary_and_events(repo):
    sid = local_session(repo, events=[(1, "car", "a_to_b"), (2, "person", "b_to_a"), (3, "car", "a_to_b")],
                        recordings=[("atcs_1.mp4", True)])
    frozen = d.build_session(repo.get_session(sid), repo.crossing_events(sid), iso())
    s = backend_schema()
    out, _, _ = s.validate_summary(frozen.summary)
    assert out == frozen.summary
    assert frozen.summary["stats_complete"] is True and frozen.summary["status"] == "completed"
    assert frozen.summary["crossing_event_count"] == 3 and frozen.summary["crossings"]["car"] == 2
    assert frozen.summary["frames_processed"] == 250 and frozen.summary["duration_s"] == 12.5
    assert frozen.summary["recordings"] == [{"file": "atcs_1.mp4", "ok": True, "duration_s": 2.5, "codec": "avc1"}]
    assert frozen.summary["source_id"] == "Integrated Camera"
    pages = list(d.event_pages(frozen.envelope, frozen.events))
    assert len(pages) == 1
    ev_out, _, _ = s.validate_events(pages[0][1])
    assert [e["track_id"] for e in ev_out["events"]] == [1, 2, 3]
    # original timestamps are kept exactly
    assert [e["timestamp"] for e in ev_out["events"]] == [e["ts"] for e in repo.crossing_events(sid)]
    # the summary does not carry the events (they are uploaded first, in pages)
    assert "crossing_events" not in frozen.summary


def test_crashed_session_is_marked_incomplete(repo):
    sid = local_session(repo, status="running", events=[(1, "car", "a_to_b"), (2, "car", "b_to_a")])
    repo.mark_interrupted_sessions()  # what app.main does at the next start
    frozen = d.build_session(repo.get_session(sid), repo.crossing_events(sid), iso())
    summary = frozen.summary
    backend_schema().validate_summary(summary)
    assert summary["status"] == "interrupted" and summary["stats_complete"] is False
    # only what is known: the saved events; nothing else is invented
    assert summary["crossings"]["car"] == 2 and summary["crossing_event_count"] == 2
    assert sum(summary["unique"].values()) == 0 and summary["frames_processed"] == 0
    assert summary["avg_inference_fps"] is None
    assert summary["line_totals"]["line-1"]["a_to_b"]["car"] == 1
    assert summary["line_totals"]["line-1"]["b_to_a"]["car"] == 1


def test_error_session_with_missing_events_is_incomplete(repo):
    sid = local_session(repo, status="error", events=[(1, "car", "a_to_b")], crossings={"car": 2})
    summary = d.build_session(repo.get_session(sid), repo.crossing_events(sid), iso()).summary
    backend_schema().validate_summary(summary)
    assert summary["stats_complete"] is False and summary["crossings"]["car"] == 1


def test_completed_session_with_missing_events_is_reported_as_error(repo):
    sid = local_session(repo, status="completed", events=[(1, "car", "a_to_b")], crossings={"car": 2})
    frozen = d.build_session(repo.get_session(sid), repo.crossing_events(sid), iso())
    backend_schema().validate_summary(frozen.summary)
    assert frozen.summary["status"] == "error" and frozen.summary["stats_complete"] is False
    assert frozen.notes


def test_repeated_local_events_are_sent_once(repo):
    sid = local_session(repo, events=[(1, "car", "a_to_b"), (1, "car", "a_to_b")], crossings={"car": 2})
    frozen = d.build_session(repo.get_session(sid), repo.crossing_events(sid), iso())
    assert len(frozen.events) == 1 and frozen.summary["crossing_event_count"] == 1
    backend_schema().validate_summary(frozen.summary)


def test_unfinished_recording_and_video_source(repo):
    sid = local_session(repo, source=("video_file", "traffic clip.mp4"), recordings=[("r.mp4", None)])
    summary = d.build_session(repo.get_session(sid), repo.crossing_events(sid), iso()).summary
    backend_schema().validate_summary(summary)
    assert summary["source"] == "video_file" and summary["source_id"] == "traffic clip.mp4"
    assert summary["recordings"][0]["ok"] is False and summary["recordings"][0]["duration_s"] is None


def test_running_session_cannot_be_frozen(repo):
    sid = local_session(repo, status="running")
    with pytest.raises(ValueError):
        d.build_session(repo.get_session(sid), [], iso())


def test_event_pages_respect_count_and_size_limits():
    env = {"schema_version": "1.0", "kind": "crossing_events", "session_id": SID, "started_at": T0,
           "source": "camera", "source_id": "Cam"}
    events = [{"timestamp": T1, "line_id": "line-1", "track_id": i, "class": "car", "direction": "a_to_b"}
              for i in range(1234)]
    pages = list(d.event_pages(env, events))
    assert [len(p["events"]) for _, p in pages] == [500, 500, 234]
    assert pages[-1][0] == 1234
    s = backend_schema()
    for _, p in pages:
        s.validate_events(p)
    # resume in the middle
    assert [len(p["events"]) for _, p in d.event_pages(env, events, 1000)] == [234]
    # long line ids: pages are cut by size, below the backend's 128 KB limit
    big = [{**e, "line_id": "L" * 64} for e in events]
    for _, p in d.event_pages(env, big):
        assert len(json.dumps(p, separators=(",", ":")).encode()) <= 128 * 1024
        s.validate_events(p)


def fixture_documents():
    """Deterministic documents the backend tests validate too (backend/tests/test_desktop_link_contract.py)."""
    live = live_detecting(state="Recording", recording_active=True, recording_file="atcs_20261024-100000.mp4")
    events = [{"timestamp": T1, "line_id": "line-1", "track_id": 7, "class": "car", "direction": "a_to_b"},
              {"timestamp": T1, "line_id": "line-1", "track_id": 9, "class": "person", "direction": "b_to_a"}]
    session = {
        "session_id": SID, "source_type": "camera", "source_id": "0:Integrated Camera", "started_at": T0,
        "ended_at": "2026-10-24T10:05:00.000+00:00", "status": "completed", "model_name": "yolo26n.pt",
        "model_version": "yolo26n.pt sha256:9b09cc8bf347 ultralytics 8.4.184", "device": "cpu",
        "stats": {"unique": {"car": 4, "person": 2}, "crossings": {"car": 1, "person": 1},
                  "line_totals": {"line-1": {"a_to_b": {"car": 1}, "b_to_a": {"person": 1}}},
                  "duration_s": 300.0, "frames_processed": 4500, "avg_inference_fps": 15.0},
        "recordings": [{"path": "C:/Users/x/recordings/atcs_20261024-100000.mp4", "ok": True, "duration_s": 120.0,
                        "codec": "avc1"}],
    }
    local_events = [{"ts": e["timestamp"], "line_id": e["line_id"], "track_id": e["track_id"], "cls": e["class"],
                     "direction": e["direction"]} for e in events]
    frozen = d.build_session(session, local_events, T1)
    crashed = d.build_session({**session, "session_id": "a" * 32, "status": "interrupted", "stats": None,
                               "recordings": []}, local_events, T1)
    return {
        "desktop_status_idle": status(d.LiveState()),
        "desktop_status_recording": status(live, INFO, seq=2),
        "crossing_events": next(iter(d.event_pages(frozen.envelope, frozen.events)))[1],
        "session_summary_completed": frozen.summary,
        "session_summary_interrupted": crashed.summary,
    }


def test_contract_fixture_is_current():
    """The fixture file must equal what the builders produce (regenerate: ATCS_UPDATE_FIXTURES=1)."""
    import os

    docs = fixture_documents()
    s = backend_schema()
    s.validate_status(docs["desktop_status_idle"])
    s.validate_status(docs["desktop_status_recording"])
    s.validate_events(docs["crossing_events"])
    s.validate_summary(docs["session_summary_completed"])
    s.validate_summary(docs["session_summary_interrupted"])
    text = json.dumps(docs, indent=2, sort_keys=True) + "\n"
    if os.environ.get("ATCS_UPDATE_FIXTURES") == "1":
        FIXTURE.write_text(text, encoding="utf-8")
    assert FIXTURE.read_text(encoding="utf-8") == text
