"""Explicit schemas for desktop telemetry documents (app/services/desktop_schema.py)."""

import json

import pytest

from app.services import desktop_schema as schema
from tests.desktop_docs import counts, event, events, hexid, iso, lines, mutate, session_block, status, summary

SID = "3f1c0d6e9a2b4c51a7e8f00112233445"
STARTED = iso(seconds=-30)


def live_status():
    return status(hexid(), 1, session=session_block(SID, STARTED))


# ------------------------------------------------------------------ JSON
@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b'"text"', b"\xff\xfe", b'{"a": NaN}', b'{"a": Infinity}',
                                 b'{"a": 1, "a": 2}'])
def test_loads_rejects(raw):
    with pytest.raises(schema.SchemaError):
        schema.loads(raw)


def test_loads_accepts_object():
    assert schema.loads(b'{"a": [1, 2.5, null]}') == {"a": [1, 2.5, None]}


# --------------------------------------------------------------- status
def test_valid_status_with_and_without_session():
    doc, times = schema.validate_status(live_status())
    assert doc["session"]["session_id"] == SID and set(times) == {
        "sent_at", "instance_started_at", "session.started_at", "session.timestamp"}
    idle, times = schema.validate_status(status(hexid(), 0))
    assert idle["session"] is None and idle["source"] is None and set(times) == {"sent_at", "instance_started_at"}


@pytest.mark.parametrize("path,value", [
    (("schema_version",), "2.0"),
    (("kind",), "session_summary"),
    (("instance_id",), "ABCDEF"),
    (("instance_id",), "g" * 32),
    (("seq",), -1),
    (("seq",), 1.5),
    (("seq",), True),
    (("seq",), 2**53),
    (("sent_at",), "2026-10-23T12:00:00"),  # no offset
    (("sent_at",), "yesterday"),
    (("state",), "Dancing"),
    (("camera_status",), "broken"),
    (("device",), "cuda:0; rm -rf"),
    (("app", "version"), "../1.0"),
    (("source",), {"kind": "camera", "label": "C:\\Users\\alber\\cam"}),
    (("source",), {"kind": "network", "label": "cam"}),
    (("recording", "file"), "/home/alberto/recordings/a.mp4"),
    (("recording", "file"), "..\\a.mp4"),
    (("recording", "active"), "yes"),
    (("performance", "inference_fps"), -1),
    (("performance", "latency_ms"), "fast"),
    (("performance", "active_tracks"), 10**6),
    (("session", "session_id"), "short"),
    (("session", "visible", "car"), -1),
    (("session", "visible", "car"), 10**5),
    (("session", "unique", "car"), 1.0),
    (("session", "crossings", "dog"), 1),
    (("session", "crossings", "car"), ...),
    (("session", "line_totals"), {"bad/line": {"a_to_b": counts(), "b_to_a": counts()}}),
    (("session", "line_totals", "line-1", "sideways"), counts()),
    (("session", "timestamp"), iso(seconds=-3600)),  # before started_at
    (("session", "frames_processed"), -5),
    (("extra",), 1),
    (("session", "extra"), 1),
    (("performance", "extra"), 1),
    (("app", "name"), ...),
])
def test_invalid_status_is_rejected(path, value):
    with pytest.raises(schema.SchemaError):
        schema.validate_status(mutate(live_status(), path, value))


def test_session_requires_source():
    with pytest.raises(schema.SchemaError):
        schema.validate_status(mutate(live_status(), ("source",), None))


def test_sent_before_instance_start_is_rejected():
    with pytest.raises(schema.SchemaError):
        schema.validate_status(status(hexid(), 1, instance_started_at=iso(seconds=10), sent_at=iso()))


def test_too_many_lines_rejected():
    many = {f"line-{i}": {"a_to_b": counts(), "b_to_a": counts()} for i in range(schema.MAX_LINES + 1)}
    with pytest.raises(schema.SchemaError):
        schema.validate_status(mutate(live_status(), ("session", "line_totals"), many))


def test_free_text_is_cleaned_and_paths_reduced():
    doc = status(hexid(), 1, message="Cannot open C:\\Users\\alber\\Videos\\clip.mp4\x00 now",
                 last_error="read /home/alberto/monitor/data/x.mp4 failed; see \\\\srv\\share\\log.txt")
    out, _ = schema.validate_status(doc)
    assert out["message"] == "Cannot open clip.mp4 now"
    assert out["last_error"] == "read x.mp4 failed; see log.txt"
    for text in (out["message"], out["last_error"]):
        assert "Users" not in text and "/home" not in text and "\\" not in text
    long = schema.validate_status(status(hexid(), 1, message="x" * 1000))[0]["message"]
    assert len(long) == schema.MAX_TEXT
    with pytest.raises(schema.SchemaError):
        schema.validate_status(status(hexid(), 1, message="x" * 5000))


def test_unicode_camera_names_are_allowed():
    doc = mutate(live_status(), ("source",), {"kind": "camera", "label": "Caméra intégrée"})
    assert schema.validate_status(doc)[0]["source"]["label"] == "Caméra intégrée"


# --------------------------------------------------------------- events
def test_valid_events_page():
    doc, started, times = schema.validate_events(events(SID, STARTED, [event(1), event(2, cls="person")]))
    assert len(doc["events"]) == 2 and len(times) == 2


@pytest.mark.parametrize("bad", [
    {"events": []},
    {"events": [event(1)] * 2},  # same key twice in one page
    {"events": [mutate(event(1), ("class",), "dog")]},
    {"events": [mutate(event(1), ("direction",), "up")]},
    {"events": [mutate(event(1), ("track_id",), -1)]},
    {"events": [mutate(event(1), ("track_id",), "7")]},
    {"events": [mutate(event(1), ("line_id",), "../x")]},
    {"events": [mutate(event(1), ("timestamp",), "2026-10-23 12:00")]},
    {"events": [mutate(event(1), ("extra",), 1)]},
    {"source_id": "C:\\video.mp4"},
    {"kind": "events"},
])
def test_invalid_events_are_rejected(bad):
    doc = events(SID, STARTED, [event(1)])
    doc.update(bad)
    with pytest.raises(schema.SchemaError):
        schema.validate_events(doc)


def test_events_page_size_limit():
    page = [event(i) for i in range(schema.MAX_EVENTS_PER_PAGE + 1)]
    with pytest.raises(schema.SchemaError):
        schema.validate_events(events(SID, STARTED, page))


# -------------------------------------------------------------- summary
def test_valid_summary():
    doc, started, ended = schema.validate_summary(summary(SID, STARTED, crossings=counts(car=2)))
    assert doc["crossing_event_count"] == 2 and ended >= started


@pytest.mark.parametrize("path,value", [
    (("status",), "running"),
    (("crossing_event_count",), 3),  # crossings add up to 0
    (("ended_at",), iso(seconds=-3600)),  # before started_at
    (("ended_at",), None),
    (("source_id",), "/home/alberto/videos/a.mp4"),
    (("recordings",), [{"file": "C:\\rec\\a.mp4", "ok": True, "duration_s": 1.0, "codec": "avc1"}]),
    (("recordings",), [{"file": "a.mp4", "ok": True, "duration_s": 1.0, "codec": "avc1", "path": "/x"}]),
    (("model", "version"), "C:\\models\\yolo.pt"),
    (("model", "name"), "../yolo.pt"),
    (("duration_s",), float("inf")),
    (("unique", "car"), -1),
    (("crossing_events",), []),  # events are uploaded in pages, not in the summary
])
def test_invalid_summary_is_rejected(path, value):
    with pytest.raises(schema.SchemaError):
        schema.validate_summary(mutate(summary(SID, STARTED), path, value))


def test_completed_summary_must_be_complete():
    with pytest.raises(schema.SchemaError):
        schema.validate_summary(summary(SID, STARTED, stats_complete=False))
    schema.validate_summary(summary(SID, STARTED, status="interrupted", stats_complete=False))


def test_canonical_is_order_independent():
    a = schema.validate_summary(summary(SID, STARTED, line_totals=lines(a_car=1)))[0]
    b = json.loads(json.dumps(a, sort_keys=False))
    b = dict(reversed(list(b.items())))
    assert schema.canonical(a) == schema.canonical(b)
