"""Desktop link sender and outbox: ordering, retries, error handling, durability.

The backend is a scripted FakeClient that validates every document with the
backend's own schema; time is a controllable clock, so no test sleeps.
test_desktop_link_backend.py repeats the main flows against the real Flask app.
"""

import logging
import threading
import time

import pytest

from app.desktop_link import documents as d
from app.desktop_link.client import NetworkError, Response, parse_retry_after
from app.desktop_link.outbox import Outbox
from app.desktop_link.service import MAX_BACKOFF_S, LinkService, backoff
from app.storage.repository import Repository, iso
from tests.link_support import TOKEN, FakeClient, link_config, local_session


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def env(tmp_path):
    repo = Repository(tmp_path / "history.sqlite3")
    box = Outbox(tmp_path / "outbox.sqlite3")
    clock = Clock()
    client = FakeClient()
    svc = LinkService(link_config(), repo, box, "1.0.0", client=client, clock=clock)

    class Env:
        pass

    e = Env()
    e.repo, e.box, e.clock, e.client, e.svc, e.tmp = repo, box, clock, client, svc, tmp_path
    yield e
    box.close()
    repo.close()


def run(e, steps=50, dt=0.35):
    for _ in range(steps):
        e.svc.run_once()
        e.clock.advance(dt)


def finish(e, sid):
    e.svc.session_finished(sid)


def live_for(sid, **kw):
    base = dict(state="Detecting", session_id=sid, timestamp=iso(), camera_status="ok", unique={"car": 1},
                visible={"car": 1}, frames_processed=10)
    base.update(kw)
    return d.LiveState(**base)


# ------------------------------------------------------------------ heartbeats
def test_heartbeats_idle_and_running(env):
    env.svc.run_once()
    assert env.client.kinds() == ["status"]
    doc = env.client.calls[0][2]
    assert doc["state"] == "Idle" and doc["session"] is None and doc["seq"] == 1
    assert doc["instance_id"] == env.svc.instance_id
    env.clock.advance(5)
    env.svc.run_once()
    assert env.client.kinds() == ["status"]  # idle: every 10 s
    env.clock.advance(5.1)
    env.svc.run_once()
    assert len(env.client.calls) == 2

    sid = local_session(env.repo, status="running")
    env.svc.set_live(live_for(sid))
    env.svc.run_once()
    doc = env.client.calls[-1][2]
    assert doc["session"]["session_id"] == sid and doc["source"]["label"] == "Integrated Camera"
    assert doc["session"]["started_at"] == env.repo.get_session(sid)["started_at"]
    n = len(env.client.calls)
    env.clock.advance(1.0)
    env.svc.run_once()
    assert len(env.client.calls) == n + 1  # running: every second
    seqs = [c[2]["seq"] for c in env.client.calls]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert env.svc.status()["connection"] == "connected"
    assert env.box.state_of(sid) == "open"  # the running session is journaled


def test_finished_session_is_not_reported_live(env):
    sid = local_session(env.repo, status="running")
    env.svc.set_live(live_for(sid))
    finish(env, sid)
    env.svc.run_once()
    assert env.client.calls[-1][2]["session"] is None


def test_session_already_finished_locally_is_not_reported_live(env):
    sid = local_session(env.repo)  # completed in the history, snapshot still shows it
    env.svc.set_live(live_for(sid))
    env.svc.run_once()
    assert env.client.calls[-1][2]["session"] is None
    run(env, steps=60, dt=1.0)
    assert env.box.state_of(sid) is None  # not something this app ran: never uploaded


def test_new_process_gets_new_identity(env, tmp_path):
    other = LinkService(link_config(), env.repo, env.box, "1.0.0", client=env.client, clock=env.clock)
    assert other.instance_id != env.svc.instance_id
    assert len(other.instance_id) == 32


def test_heartbeat_dropped_while_offline(env):
    env.client.respond("status", NetworkError("backend unreachable"))
    env.svc.run_once()
    assert env.svc.status()["connection"] == "offline"
    env.clock.advance(1)
    env.svc.run_once()
    assert len(env.client.calls) == 1  # dropped, not queued: next try with the next heartbeat (10 s idle)
    env.clock.advance(9.5)
    env.svc.run_once()
    assert len(env.client.calls) == 2 and env.svc.status()["connection"] == "connected"
    # while monitoring, a failed heartbeat is retried after 5 s instead of every second
    env.svc.set_live(d.LiveState(state="Detecting"))
    env.svc.run_once()
    n = len(env.client.calls)
    env.client.respond("status", NetworkError("backend unreachable"))
    env.clock.advance(1.0)
    env.svc.run_once()
    env.clock.advance(4.0)
    env.svc.run_once()
    assert len(env.client.calls) == n + 1
    env.clock.advance(1.1)
    env.svc.run_once()
    assert len(env.client.calls) == n + 2


@pytest.mark.parametrize("status,code,state", [(401, "device_unauthorized", "unauthorized"),
                                               (403, "origin_refused", "refused"),
                                               (404, "", "incompatible")])
def test_configuration_errors_pause_everything(env, status, code, state):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond("status", Response(status, {"code": code}))
    env.svc.run_once()
    assert env.svc.status()["connection"] == state
    n = len(env.client.calls)
    run(env, steps=20, dt=1.0)  # 20 s: still paused
    assert len(env.client.calls) == n
    assert env.box.rows()[0]["attempts"] == 0  # nothing was counted as a failed upload
    env.clock.advance(60)
    run(env, steps=10)
    assert env.box.state_of(sid) == "done"


def test_rate_limit_respects_retry_after(env):
    sid = local_session(env.repo)
    env.box.observe(sid)
    env.client.respond("status", Response(429, {"code": "rate_limited"}, retry_after=7.0))
    env.svc.run_once()
    assert env.svc.status()["connection"] == "rate_limited"
    n = len(env.client.calls)
    for _ in range(6):
        env.clock.advance(1.0)
        env.svc.run_once()
    assert len(env.client.calls) == n  # nothing at all is sent during the pause
    env.clock.advance(1.5)
    env.svc.run_once()
    assert [k for k, _, _ in env.client.calls[n:]] == ["summary"]


@pytest.mark.parametrize("value,expected", [("7", 7.0), ("0", 1.0), ("100000", 300.0), ("soon", None), (None, None)])
def test_parse_retry_after(value, expected):
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    now = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now) == 30.0


def test_superseded_heartbeat_is_reported(env):
    env.client.respond("status", Response(200, {"accepted": False, "reason": "superseded"}))
    env.svc.run_once()
    assert "another ATCS Monitor process" in env.svc.status()["detail"]


# ------------------------------------------------------------------- uploads
def test_session_upload_events_before_summary(env):
    events = [(i, "car" if i % 3 else "person", "a_to_b") for i in range(1, 1201)]
    sid = local_session(env.repo, events=events, recordings=[("r.mp4", True)])
    env.box.observe(sid)
    run(env, steps=30)
    kinds = [k for k, s, _ in env.client.calls if s == sid]
    assert kinds == ["events", "events", "events", "summary"]
    sizes = [len(doc["events"]) for k, s, doc in env.client.calls if k == "events"]
    assert sizes == [500, 500, 200]
    assert env.box.state_of(sid) == "done"
    assert env.client.summaries[sid]["crossing_event_count"] == 1200
    row = env.box._exec("SELECT events_json, summary_json FROM uploads WHERE session_id=?", (sid,)).fetchone()
    assert row["events_json"] is None and row["summary_json"] is None  # payload freed once acknowledged


def test_upload_rate_stays_below_backend_limit(env):
    events = [(i, "car", "a_to_b") for i in range(1, 5001)]
    sids = [local_session(env.repo, events=events) for _ in range(2)]
    for sid in sids:
        env.box.observe(sid)
    start = env.clock()
    sent_at = []
    for _ in range(400):
        n = len(env.client.calls)
        env.svc.run_once()
        if len(env.client.calls) > n:
            sent_at.append(env.clock())
        env.clock.advance(0.05)
    assert all(env.box.state_of(s) == "done" for s in sids)
    # no 1-second window ever holds more than 5 requests (backend: 5/s sustained)
    for t in sent_at:
        assert sum(1 for u in sent_at if t <= u < t + 1.0) <= 5
    assert env.clock() > start


def test_session_without_events_uploads_summary_only(env):
    sid = local_session(env.repo)
    env.box.observe(sid)
    run(env)
    assert [k for k, s, _ in env.client.calls if s == sid] == ["summary"]
    assert env.box.state_of(sid) == "done"


def test_waits_for_recording_validation(env):
    sid = local_session(env.repo, recordings=[("r.mp4", None)])  # finalizer still running
    env.box.observe(sid)
    run(env, steps=10)
    assert env.box.state_of(sid) == "open"
    rec_id = env.repo.get_session(sid)["recordings"][0]["id"]
    env.repo.finish_recording(rec_id, {"ok": True, "duration_s": 3.0, "frames_written": 30})
    env.svc.poke()
    run(env, steps=10)
    assert env.box.state_of(sid) == "done"
    assert env.client.summaries[sid]["recordings"][0]["ok"] is True


def test_recording_grace_period_ends(env):
    sid = local_session(env.repo, recordings=[("r.mp4", None)])
    env.box.observe(sid)
    run(env, steps=5)
    assert env.box.state_of(sid) == "open"
    env.clock.advance(130)
    env.svc.poke()
    run(env, steps=10)
    assert env.box.state_of(sid) == "done"
    assert env.client.summaries[sid]["recordings"][0]["ok"] is False


def test_transient_failures_back_off_and_recover(env):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond("events", Response(503, {}), Response(500, {}), NetworkError("down"))
    run(env, steps=3, dt=0.4)
    rows = env.box.rows()
    assert rows[0]["state"] == "pending" and rows[0]["attempts"] >= 1
    run(env, steps=400, dt=1.0)
    assert env.box.state_of(sid) == "done"


def test_backoff_is_bounded():
    assert all(backoff(n) <= MAX_BACKOFF_S * 1.2 for n in range(50))
    assert backoff(0) < 3


def test_retry_after_outage_skips_remaining_backoff(env):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond("status", *[NetworkError("down")] * 30)
    env.client.respond("events", *[NetworkError("down")] * 6)
    run(env, steps=200, dt=0.5)  # 100 s outage
    assert env.box.rows()[0]["next_attempt_at"] > env.clock() + 10  # long backoff by now
    env.client.script["status"].clear()  # the backend is back
    env.clock.advance(10)
    run(env, steps=10)  # the next heartbeat succeeds and the upload follows at once
    assert env.box.state_of(sid) == "done"


@pytest.mark.parametrize("kind,status,code", [
    ("summary", 409, "summary_conflict"),
    ("summary", 409, "session_conflict"),
    ("summary", 422, "counter_decreased"),
    ("events", 409, "event_conflict"),
    ("events", 422, "invalid_timestamp"),
    ("events", 400, "invalid_document"),
    ("summary", 413, "payload_too_large"),
])
def test_permanent_refusals_are_kept_not_retried(env, kind, status, code, caplog):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond(kind, Response(status, {"code": code, "error": "nope"}))
    with caplog.at_level(logging.ERROR):
        run(env, steps=20)
    assert env.box.state_of(sid) == "rejected"
    n = len(env.client.calls)
    run(env, steps=100, dt=10)
    assert [c for c in env.client.calls[n:] if c[0] != "status"] == []  # not retried
    row = env.box._exec("SELECT summary_json, last_code FROM uploads WHERE session_id=?", (sid,)).fetchone()
    assert row["summary_json"] is not None and row["last_code"] == code  # kept, with the reason
    assert any(code in r.getMessage() for r in caplog.records)
    assert env.svc.status()["connection"] == "connected"
    # an explicit retry sends it again
    assert env.box.retry_rejected(sid) == 1
    run(env, steps=20)
    assert env.box.state_of(sid) == "done"


def test_clock_skew_is_retried(env):
    sid = local_session(env.repo)
    env.box.observe(sid)
    env.client.respond("summary", Response(422, {"code": "clock_skew"}))
    run(env, steps=5)
    assert env.box.state_of(sid) == "pending"
    run(env, steps=50, dt=1.0)
    assert env.box.state_of(sid) == "done"


def test_events_incomplete_resends_events(env):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b"), (2, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond("summary", Response(409, {"code": "events_incomplete", "stored_events": 1}))
    run(env, steps=20)
    kinds = [k for k, s, _ in env.client.calls if s == sid]
    assert kinds == ["events", "summary", "events", "summary"]
    assert env.box.state_of(sid) == "done"


def test_events_incomplete_gives_up_after_two_resends(env):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond("summary", *[Response(409, {"code": "events_incomplete", "stored_events": 0})] * 3)
    run(env, steps=40)
    assert env.box.state_of(sid) == "rejected"


def test_duplicate_upload_does_not_duplicate(env):
    """A crash after the backend stored everything, but before the outbox noted it."""
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    run(env)
    assert env.box.state_of(sid) == "done"
    env.box._exec("UPDATE uploads SET state='rejected' WHERE session_id=?", (sid,))
    env.box.retry_rejected(sid)
    env.box._exec("UPDATE uploads SET envelope_json=?, events_json=?, summary_json=? WHERE session_id=?", (
        *[__import__("json").dumps(x) for x in _frozen(env, sid)], sid))
    run(env)
    assert env.box.state_of(sid) == "done"
    assert len(env.client.events[sid]) == 1 and len(env.client.summaries) == 1


def _frozen(env, sid):
    f = d.build_session(env.repo.get_session(sid), env.repo.crossing_events(sid), iso())
    return f.envelope, f.events, env.client.summaries and next(
        c[2] for c in env.client.calls if c[0] == "summary" and c[1] == sid)


def test_frozen_summary_never_changes_between_retries(env):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond("summary", Response(503, {}), Response(503, {}))
    run(env, steps=200, dt=1.0)
    sent = [c[2] for c in env.client.calls if c[0] == "summary"]
    assert len(sent) == 3 and sent[0] == sent[1] == sent[2]


def test_outbox_survives_restart_and_resumes_events(env, tmp_path):
    events = [(i, "car", "a_to_b") for i in range(1, 1101)]
    sid = local_session(env.repo, events=events)
    env.box.observe(sid)
    env.client.respond("events", Response(200, {"inserted": 500}), NetworkError("down"))
    run(env, steps=6, dt=0.4)
    assert env.box.rows()[0]["events_sent"] == 500
    env.box.close()

    # the app restarts: new process, new outbox connection, new instance
    box2 = Outbox(tmp_path / "outbox.sqlite3")
    client2 = FakeClient()
    svc2 = LinkService(link_config(), env.repo, box2, "1.0.0", client=client2, clock=env.clock)
    for _ in range(30):
        svc2.run_once()
        env.clock.advance(0.4)
    pages = [doc for k, _, doc in client2.calls if k == "events"]
    assert [len(p["events"]) for p in pages[:2]] == [500, 100]  # resumed after the first page
    assert pages[0]["events"][0]["track_id"] == 501
    # client2 only saw 600 of 1100 events, so it answers events_incomplete and all are resent
    assert box2.state_of(sid) == "done"
    box2.close()
    env.box = Outbox(tmp_path / "outbox.sqlite3")


def test_crash_recovery_uploads_interrupted_session(env, tmp_path):
    sid = local_session(env.repo, status="running", events=[(1, "car", "a_to_b"), (2, "person", "a_to_b")])
    env.svc.set_live(live_for(sid, crossings={"car": 1, "person": 1}))
    env.svc.run_once()
    assert env.box.state_of(sid) == "open"
    # crash: nothing else happens in this process. Next start:
    env.repo.mark_interrupted_sessions()
    svc2 = LinkService(link_config(), env.repo, env.box, "1.0.0", client=env.client, clock=env.clock)
    for _ in range(20):
        svc2.run_once()
        env.clock.advance(0.4)
    assert env.box.state_of(sid) == "done"
    summary = env.client.summaries[sid]
    assert summary["status"] == "interrupted" and summary["stats_complete"] is False
    assert summary["crossing_event_count"] == 2


def test_unobserved_sessions_are_not_uploaded(env):
    """Sessions from before the link, or from the separate monitor service, stay local."""
    local_session(env.repo, events=[(1, "car", "a_to_b")])
    run(env)
    assert env.client.kinds() and set(env.client.kinds()) == {"status"}


def test_reset_counters_creates_two_sessions(env):
    a = local_session(env.repo, status="running")
    env.svc.set_live(live_for(a))
    env.svc.run_once()
    env.repo.finish_session(a, "completed", {"unique": {}, "crossings": {}, "duration_s": 1})
    finish(env, a)
    b = local_session(env.repo, status="running")
    env.svc.set_live(live_for(b))
    run(env, steps=10)
    assert env.box.state_of(a) == "done" and env.box.state_of(b) == "open"
    assert env.client.calls[-1][2]["session"]["session_id"] == b


def test_stop_flushes_ready_sessions(env):
    sid = local_session(env.repo, status="running")
    env.svc.set_live(live_for(sid))
    env.svc.run_once()
    env.repo.finish_session(sid, "completed", {"unique": {}, "crossings": {}, "duration_s": 1})
    env.svc.session_finished(sid)
    env.svc.set_live(d.LiveState(state="Idle", message="Stopped"))
    env.svc.clock = time.time
    env.svc.start()
    env.svc.stop(flush_s=3.0)
    env.svc.join(10)
    assert not env.svc.is_alive()
    assert env.box.state_of(sid) == "done"
    assert env.client.calls[-1][0] in ("status", "summary")
    assert any(k == "status" and doc["state"] == "Idle" for k, _, doc in env.client.calls)


def test_stop_when_offline_is_immediate(env):
    env.client.respond("status", NetworkError("down"))
    env.svc.clock = time.time
    env.svc.start()
    deadline = time.monotonic() + 5
    while env.svc.status()["connection"] != "offline" and time.monotonic() < deadline:
        time.sleep(0.01)
    t0 = time.monotonic()
    env.svc.stop(flush_s=3.0)
    env.svc.join(5)
    assert not env.svc.is_alive() and time.monotonic() - t0 < 2


def test_gui_calls_never_block_on_network(env):
    """set_live / session_finished / status return immediately while a request hangs."""
    gate = threading.Event()

    class SlowClient(FakeClient):
        def post_status(self, doc):
            gate.wait(5)
            return super().post_status(doc)

    env.svc.client = SlowClient()
    env.svc.clock = time.time
    env.svc.start()
    time.sleep(0.1)  # the thread is now stuck in the request
    t0 = time.monotonic()
    for _ in range(100):
        env.svc.set_live(d.LiveState(state="Detecting"))
        env.svc.status()
        env.svc.session_finished("a" * 32)
    assert time.monotonic() - t0 < 0.2
    gate.set()
    env.svc.stop(flush_s=0)
    env.svc.join(5)


def test_token_never_logged_or_in_errors(env, caplog):
    sid = local_session(env.repo, events=[(1, "car", "a_to_b")])
    env.box.observe(sid)
    env.client.respond("status", Response(401, {"code": "device_unauthorized"}), NetworkError("down"))
    env.client.respond("events", Response(500, {}), Response(409, {"code": "event_conflict", "error": "x"}))
    with caplog.at_level(logging.DEBUG):
        run(env, steps=300, dt=1.0)
    text = caplog.text + repr(env.svc.status()) + repr(env.box.rows()) + repr(env.svc.config)
    assert TOKEN not in text
    assert "atcsdev_" not in text
