"""SQLite lifecycle, CSV/JSON export and the integration contract."""

import csv
import json
import sqlite3

import pytest

from app.detection.classes import zero_counts
from app.integration import schemas
from app.storage.export import csv_safe, export_session_csv, export_session_json
from app.storage.repository import EventWriter, Repository, StorageError, iso


def counts(**kw):
    c = zero_counts()
    c.update(kw)
    return c


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "t.sqlite3")
    yield r
    r.close()


def make_session(repo, sid="s1", source="camera"):
    repo.create_session(sid, source, "0:Webcam", "yolo26n.pt", "v", "cpu", {"confidence": 0.35})


def finish(repo, sid, crossings, unique=None, status="completed"):
    repo.finish_session(sid, status, {"unique": unique or counts(), "crossings": crossings,
                                      "line_totals": {}, "duration_s": 3.0, "frames_processed": 30})


def test_session_lifecycle_and_events(repo):
    make_session(repo)
    w = EventWriter(repo, flush_interval=0.05)
    w.start()
    w.add_crossing(("s1", iso(), "line-1", 4, "car", "a_to_b"))
    w.add_crossing(("s1", iso(), "line-1", 5, "person", "b_to_a"))
    w.add_detection(("s1", iso(), "track_confirmed", "car", 0.9, 4, 1, 2, 3, 4))
    w.flush_and_stop()
    assert w.written == 3 and w.failed == 0
    finish(repo, "s1", counts(car=1, person=1), counts(car=1, person=1))
    s = repo.get_session("s1")
    assert s["status"] == "completed" and s["ended_at"]
    assert s["stats"]["crossings"]["car"] == 1
    assert [e["cls"] for e in repo.crossing_events("s1")] == ["car", "person"]
    assert repo.detection_events("s1")[0]["event_type"] == "track_confirmed"
    assert repo.list_sessions()[0]["session_id"] == "s1"


def test_interrupted_sessions_marked_on_restart(tmp_path):
    r = Repository(tmp_path / "db.sqlite3")
    make_session(r, "crashed")
    r.add_recording("crashed", "/x/rec.mp4", "mp4v", "mp4", iso())
    r.close()  # simulates a crash: never finished
    r2 = Repository(tmp_path / "db.sqlite3")
    assert r2.mark_interrupted_sessions() == 1
    s = r2.get_session("crashed")
    assert s["status"] == "interrupted"
    assert "not finalized" in s["recordings"][0]["error"]
    r2.close()


def test_transaction_rolls_back_on_error(repo):
    make_session(repo)
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_events([("s1", iso(), "l", 1, "car", "a_to_b"), ("s1", None, "l", 1, "car", "a_to_b")], [])
    assert repo.crossing_events("s1") == []  # all-or-nothing


def test_event_for_unknown_session_rejected(repo):
    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_events([("nope", iso(), "l", 1, "car", "a_to_b")], [])


def test_unwritable_database_reports_error(tmp_path):
    with pytest.raises(StorageError):
        Repository(tmp_path / "missing_dir" / "x.sqlite3")


def test_recording_rows(repo):
    make_session(repo)
    rid = repo.add_recording("s1", "/r/a.mp4", "mp4v", "mp4", iso())
    repo.finish_recording(rid, {"frames_written": 10, "duration_s": 1.0, "ok": True, "error": None})
    rec = repo.get_session("s1")["recordings"][0]
    assert rec["ok"] is True and rec["frames"] == 10


def test_exports(repo, tmp_path):
    make_session(repo)
    repo.insert_events([("s1", iso(), "=HYPERLINK(1)", 3, "car", "a_to_b")], [])
    finish(repo, "s1", counts(car=1), counts(car=2))
    jp = export_session_json(repo, "s1", tmp_path / "out" / "s.json")
    doc = json.loads(jp.read_text())
    schemas.validate_session_summary(doc)
    assert doc["crossings"]["car"] == 1 and doc["unique"]["car"] == 2
    assert doc["crossing_events"][0]["track_id"] == 3
    summ, ev = export_session_csv(repo, "s1", tmp_path / "out")
    rows = list(csv.DictReader(summ.open()))
    assert [r["metric"] for r in rows] == ["unique", "crossings"]
    assert rows[1]["car"] == "1" and rows[1]["total"] == "1"
    erows = list(csv.DictReader(ev.open()))
    assert list(erows[0].keys()) == ["session_id", "timestamp", "line_id", "track_id", "class", "direction"]
    assert erows[0]["line_id"].startswith("'=")  # formula injection neutralised
    assert erows[0]["timestamp"].endswith("+00:00")


def test_export_unknown_session(repo, tmp_path):
    with pytest.raises(StorageError):
        export_session_json(repo, "missing", tmp_path / "x.json")


@pytest.mark.parametrize("v,exp", [("=1+1", "'=1+1"), ("+x", "'+x"), ("-x", "'-x"), ("@x", "'@x"), ("ok", "ok"), (5, 5)])
def test_csv_safe(v, exp):
    assert csv_safe(v) == exp


# ---------------------------------------------------------------- contract
def test_snapshot_roundtrip():
    doc = schemas.build_snapshot("sess", "2026-10-23T12:00:00Z", "camera", counts(car=3), counts(person=4))
    assert doc["schema_version"] == "1.0"
    assert list(doc["visible"]) == ["person", "car", "motorcycle", "bicycle", "bus", "truck"]
    assert schemas.snapshot_from_json(schemas.to_json(doc)) == doc


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(schema_version="2.0"),
        lambda d: d.update(session_id=""),
        lambda d: d.update(timestamp="2026-10-23T12:00:00"),  # no timezone
        lambda d: d.update(timestamp="yesterday"),
        lambda d: d.update(source="drone"),
        lambda d: d["visible"].update(car=-1),
        lambda d: d["visible"].update(car=1.5),
        lambda d: d["visible"].update(car=True),
        lambda d: d["crossings"].update(dog=1),
        lambda d: d["crossings"].pop("bus"),
    ],
)
def test_snapshot_validation_rejects(mutate):
    doc = schemas.build_snapshot("s", "2026-10-23T12:00:00+00:00", "video_file", counts(), counts())
    mutate(doc)
    with pytest.raises(schemas.ContractError):
        schemas.validate_snapshot(doc)


def test_snapshot_from_bad_json():
    with pytest.raises(schemas.ContractError):
        schemas.snapshot_from_json("{")


def test_session_summary_totals_must_match_events(repo):
    make_session(repo)
    repo.insert_events([("s1", iso(), "line-1", 3, "car", "a_to_b")], [])
    finish(repo, "s1", counts(car=2))  # inconsistent on purpose
    with pytest.raises(schemas.ContractError, match="do not match"):
        schemas.session_summary(repo.get_session("s1"), repo.crossing_events("s1"))
