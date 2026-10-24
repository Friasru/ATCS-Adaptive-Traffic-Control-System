"""Desktop telemetry: device credentials, ingestion and the read-only browser views.

Covers /api/desktop/telemetry/* (device token) and /api/monitor/desktop/*
(JWT + monitor grant). No desktop app, camera or monitor code is involved.
"""

import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest
from flask_jwt_extended import create_access_token

from app import bcrypt, create_app, db
from app.models import DesktopCrossingEvent, DesktopSession, DesktopStatus, MonitorDevice, User
from app.models.monitor_device import TOKEN_PREFIX, hash_token
from app.services import monitor_access, monitor_devices
from tests.conftest import ConfigForTesting
from tests.desktop_docs import counts, event, events, hexid, iso, lines, session_block, status, summary

STATUS_URL = "/api/desktop/telemetry/status"
READ_URLS = ["/api/monitor/desktop/status", "/api/monitor/desktop/sessions"]


def events_url(sid):
    return f"/api/desktop/telemetry/sessions/{sid}/events"


def summary_url(sid):
    return f"/api/desktop/telemetry/sessions/{sid}/summary"


def add_user(name, password="password1"):
    user = User(username=name, password_hash=bcrypt.generate_password_hash(password).decode())
    db.session.add(user)
    db.session.commit()
    return user


def make_app(config=ConfigForTesting):
    app = create_app(config)
    with app.app_context():
        for name in ("op_user", "view_user", "stranger"):
            add_user(name)
        monitor_access.grant("op_user", "operator")
        monitor_access.grant("view_user", "viewer")
        tokens = {name: monitor_devices.add(name) for name in ("laptop-a", "laptop-b")}
    return SimpleNamespace(app=app, client=app.test_client(), a=tokens["laptop-a"], b=tokens["laptop-b"])


@pytest.fixture
def t():
    env = make_app()
    yield env
    with env.app.app_context():
        db.session.remove()
        db.drop_all()


def post(env, url, doc, token, raw=None, headers=None, **kw):
    hdrs = {"Authorization": f"Bearer {token}"} if token else {}
    hdrs.update(headers or {})
    data = raw if raw is not None else json.dumps(doc)
    return env.client.post(url, data=data, content_type=kw.pop("content_type", "application/json"), headers=hdrs, **kw)


def jwt_headers(env, username, **kw):
    with env.app.app_context():
        uid = str(User.query.filter_by(username=username).one().id)
        return {"Authorization": f"Bearer {create_access_token(identity=uid, **kw)}"}


def read(env, url, username="view_user"):
    return env.client.get(url, headers=jwt_headers(env, username))


def live(sid, started, seq, inst, instance_started_at=None, **kw):
    return status(inst, seq, session=session_block(sid, started, **kw), instance_started_at=instance_started_at)


def stored_session(env, sid):
    with env.app.app_context():
        s = db.session.get(DesktopSession, sid)
        return None if s is None else s.to_dict(detail=True)


def age_status(env, device_name, seconds):
    """Pretend the device's last heartbeat arrived `seconds` ago."""
    with env.app.app_context():
        dev = MonitorDevice.query.filter_by(name=device_name).one()
        row = db.session.get(DesktopStatus, dev.id)
        row.received_at = row.received_at - timedelta(seconds=seconds)
        db.session.commit()


# ============================================================ devices
def test_device_token_is_strong_and_only_its_hash_is_stored(t):
    assert t.a.startswith(TOKEN_PREFIX) and len(t.a) >= len(TOKEN_PREFIX) + 43 and t.a != t.b
    with t.app.app_context():
        dev = MonitorDevice.query.filter_by(name="laptop-a").one()
        assert dev.token_hash == hash_token(t.a) and t.a not in json.dumps(dev.to_dict())
        for column in MonitorDevice.__table__.columns:
            assert getattr(dev, column.name) != t.a


@pytest.mark.parametrize("name", ["ab", "has space", "x" * 65, "../etc", "", None])
def test_invalid_device_names_rejected(t, name):
    with t.app.app_context(), pytest.raises(ValueError):
        monitor_devices.add(name)


def test_duplicate_and_revoked_names_cannot_be_reused(t):
    with t.app.app_context():
        with pytest.raises(ValueError, match="active"):
            monitor_devices.add("laptop-a")
        assert monitor_devices.revoke("laptop-a") is True
        assert monitor_devices.revoke("laptop-a") is False
        assert monitor_devices.revoke("nobody") is False
        with pytest.raises(ValueError, match="revoked"):
            monitor_devices.add("laptop-a")


def test_device_script(tmp_path, capsys):
    import monitor_access as script

    class FileConfig(ConfigForTesting):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "devices.db")

    assert script.main(["device", "add", "win-laptop"], FileConfig) == 0
    out = capsys.readouterr().out
    token = next(w for w in out.split() if w.startswith(TOKEN_PREFIX))
    assert script.main(["device", "add", "win-laptop"], FileConfig) == 1
    assert "already exists" in capsys.readouterr().err
    assert script.main(["device", "add", "x"], FileConfig) == 1
    assert script.main(["device", "list"], FileConfig) == 0
    listed = capsys.readouterr().out
    assert "win-laptop" in listed and "active" in listed and token not in listed and hash_token(token) not in listed
    assert script.main(["device", "revoke", "win-laptop"], FileConfig) == 0
    assert script.main(["device", "revoke", "win-laptop"], FileConfig) == 1
    assert script.main(["device", "list"], FileConfig) == 0
    assert "REVOKED" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        script.main(["device", "rename", "win-laptop"], FileConfig)
    app = create_app(FileConfig)
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
    raw = (tmp_path / "devices.db").read_bytes()
    assert token.encode() not in raw and hash_token(token).encode() in raw


def test_no_endpoint_creates_devices(t):
    """Nothing reachable over HTTP issues a credential."""
    rules = [r for r in t.app.url_map.iter_rules() if "desktop" in r.rule]
    assert sorted(r.rule for r in rules) == sorted([
        "/api/desktop/telemetry/status", "/api/desktop/telemetry/sessions/<session_id>/events",
        "/api/desktop/telemetry/sessions/<session_id>/summary", "/api/monitor/desktop/status",
        "/api/monitor/desktop/sessions", "/api/monitor/desktop/sessions/<session_id>"])
    for r in rules:
        if r.rule.startswith("/api/monitor/"):
            assert r.methods - {"HEAD", "OPTIONS"} == {"GET"}
    for url in ("/api/desktop/devices", "/api/monitor/desktop/devices", "/api/desktop/telemetry/register"):
        assert t.client.post(url, json={"name": "evil"}, headers=jwt_headers(t, "op_user")).status_code in (404, 405)
    with t.app.app_context():
        assert MonitorDevice.query.count() == 2


# ===================================================== authentication
BAD_AUTH = [None, "", "Basic abc", "Bearer", "Bearer ", "Bearer atcsdev_wrong", "Bearer " + TOKEN_PREFIX + "x" * 200,
            "bearer {a}", "Bearer {a} extra", "Token {a}"]


@pytest.mark.parametrize("header", BAD_AUTH)
def test_missing_or_invalid_device_token_is_401(t, header):
    sid, started = hexid(), iso(seconds=-5)
    for url, doc in ((STATUS_URL, status(hexid(), 1)), (events_url(sid), events(sid, started, [event(1)])),
                     (summary_url(sid), summary(sid, started))):
        hdrs = {} if header is None else {"Authorization": header.format(a=t.a)}
        r = post(t, url, doc, None, headers=hdrs)
        assert r.status_code == 401, (header, url)
        assert r.get_json() == {"error": "device authentication required", "code": "device_unauthorized"}
    with t.app.app_context():
        assert DesktopStatus.query.count() == 0 and DesktopSession.query.count() == 0


def test_revoked_device_is_401_and_others_keep_working(t):
    assert post(t, STATUS_URL, status(hexid(), 1), t.a).status_code == 200
    with t.app.app_context():
        monitor_devices.revoke("laptop-a")
    assert post(t, STATUS_URL, status(hexid(), 2), t.a).status_code == 401
    assert post(t, STATUS_URL, status(hexid(), 1), t.b).status_code == 200
    names = [d["desktop"]["name"] for d in read(t, "/api/monitor/desktop/status").get_json()["devices"]]
    assert names == ["laptop-b"]


@pytest.mark.parametrize("user", ["op_user", "view_user", "stranger"])
def test_browser_jwt_cannot_write_telemetry(t, user):
    hdrs = jwt_headers(t, user)
    sid = hexid()
    for url, doc in ((STATUS_URL, status(hexid(), 1)), (events_url(sid), events(sid, iso(), [event(1)])),
                     (summary_url(sid), summary(sid, iso(seconds=-1)))):
        r = t.client.post(url, data=json.dumps(doc), content_type="application/json", headers=hdrs)
        assert r.status_code == 401 and r.get_json()["code"] == "device_unauthorized"
    with t.app.app_context():
        assert DesktopStatus.query.count() == 0 and DesktopSession.query.count() == 0


@pytest.mark.parametrize("url", ["/api/monitor/access", "/api/monitor/status", "/api/monitor/sessions",
                                 "/api/monitor/desktop/status", "/api/monitor/desktop/sessions",
                                 "/api/monitor/desktop/sessions/" + "a" * 32, "/api/compare?scenario_id=1",
                                 "/api/runs/export.csv"])
def test_device_token_cannot_use_browser_apis(t, url):
    r = t.client.get(url, headers={"Authorization": f"Bearer {t.a}"})
    assert r.status_code == 401


def test_device_token_cannot_control_the_monitor(t):
    for url in ("/api/monitor/start", "/api/monitor/stop", "/api/monitor/recording/start"):
        assert t.client.post(url, json={}, headers={"Authorization": f"Bearer {t.a}"}).status_code == 401


# ========================================================== origins
@pytest.mark.parametrize("addr", ["192.168.1.20", "172.17.0.1", "10.0.0.5", "8.8.8.8", "fe80::1", "", "unix"])
def test_non_loopback_clients_are_refused(t, addr):
    r = post(t, STATUS_URL, status(hexid(), 1), t.a, environ_base={"REMOTE_ADDR": addr})
    assert r.status_code == 403 and r.get_json()["code"] == "origin_refused"


@pytest.mark.parametrize("addr", ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"])
def test_loopback_clients_are_accepted(t, addr):
    r = post(t, STATUS_URL, status(hexid(), 1), t.a, environ_base={"REMOTE_ADDR": addr})
    assert r.status_code == 200


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host"])
def test_proxied_requests_are_refused(t, header):
    r = post(t, STATUS_URL, status(hexid(), 1), t.a, headers={header: "203.0.113.9"})
    assert r.status_code == 403 and r.get_json()["code"] == "origin_refused"


@pytest.mark.parametrize("header,value", [("Origin", "http://127.0.0.1:5000"), ("Origin", "https://evil.example"),
                                          ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "cors")])
def test_browser_requests_are_refused(t, header, value):
    r = post(t, STATUS_URL, status(hexid(), 1), t.a, headers={header: value})
    assert r.status_code == 403 and r.get_json()["code"] == "browser_refused"


def test_requests_with_cookies_are_refused(t):
    t.client.set_cookie("session", "abc")  # the test client drops a hand-written Cookie header
    r = post(t, STATUS_URL, status(hexid(), 1), t.a)
    assert r.status_code == 403 and r.get_json()["code"] == "browser_refused"


def test_no_cors_headers(t):
    for method in ("OPTIONS", "POST"):
        r = t.client.open(STATUS_URL, method=method, headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization, content-type", "Authorization": f"Bearer {t.a}"})
        assert not any(h.lower().startswith("access-control-") for h in r.headers.keys())


def test_get_on_telemetry_routes_is_405(t):
    for url in (STATUS_URL, events_url(hexid()), summary_url(hexid())):
        r = t.client.get(url, headers={"Authorization": f"Bearer {t.a}"})
        assert r.status_code == 405 and "POST" in r.headers["Allow"]


# ===================================================== request bodies
def test_content_type_must_be_json(t):
    r = post(t, STATUS_URL, status(hexid(), 1), t.a, content_type="text/plain")
    assert r.status_code == 415 and r.get_json()["code"] == "unsupported_media_type"


@pytest.mark.parametrize("url,limit", [(STATUS_URL, 8 * 1024), (events_url("a" * 32), 128 * 1024),
                                       (summary_url("a" * 32), 32 * 1024)])
def test_oversized_bodies_are_413(t, url, limit):
    body = json.dumps({"pad": "x" * limit})
    r = post(t, url, None, t.a, raw=body)
    assert r.status_code == 413 and r.get_json()["code"] == "payload_too_large"


def test_body_without_length_is_411(t):
    r = t.client.post(STATUS_URL, data=b"{}", content_type="application/json",
                      headers={"Authorization": f"Bearer {t.a}", "Transfer-Encoding": "chunked"})
    assert r.status_code == 411


@pytest.mark.parametrize("raw", ["not json", "[1]", '{"seq": NaN}', '{"a": 1, "a": 1}', " "])
def test_malformed_json_is_400(t, raw):
    r = post(t, STATUS_URL, None, t.a, raw=raw)
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_json"


def test_empty_body_is_refused(t):
    assert post(t, STATUS_URL, None, t.a, raw="").status_code in (400, 411)


def test_schema_errors_are_400_and_store_nothing(t):
    doc = status(hexid(), 1)
    doc["unexpected"] = True
    r = post(t, STATUS_URL, doc, t.a)
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_document"
    with t.app.app_context():
        assert DesktopStatus.query.count() == 0


@pytest.mark.parametrize("sid", ["ABC", "a" * 31, "../etc", "a" * 33, "Z" * 32])
def test_session_id_in_url_is_validated(t, sid):
    for url in (events_url(sid), summary_url(sid)):
        r = post(t, url, {}, t.a)
        assert r.status_code in (400, 404, 405)


def test_session_id_must_match_url(t):
    sid, other, started = hexid(), hexid(), iso(seconds=-5)
    assert post(t, events_url(other), events(sid, started, [event(1)]), t.a).status_code == 400
    assert post(t, summary_url(other), summary(sid, started), t.a).status_code == 400


# ======================================================== rate limit
def test_rate_limit_per_device():
    class Limited(ConfigForTesting):
        DESKTOP_RATE_BURST = 3
        DESKTOP_RATE_PER_S = 0.01

    env = make_app(Limited)
    inst = hexid()
    codes = [post(env, STATUS_URL, status(inst, i), env.a).status_code for i in range(1, 6)]
    assert codes == [200, 200, 200, 429, 429]
    r = post(env, STATUS_URL, status(inst, 9), env.a)
    assert r.get_json()["code"] == "rate_limited" and int(r.headers["Retry-After"]) >= 1
    assert post(env, STATUS_URL, status(hexid(), 1), env.b).status_code == 200  # other device unaffected
    with env.app.app_context():
        assert db.session.get(DesktopStatus, 1).seq == 3
        db.drop_all()


def test_token_bucket_refills():
    from app.api.desktop_telemetry import TokenBucket

    now = [0.0]
    bucket = TokenBucket(rate=2, burst=2, clock=lambda: now[0])
    assert [bucket.take("k")[0] for _ in range(3)] == [True, True, False]
    now[0] += 0.5
    assert bucket.take("k")[0] is True and bucket.take("k")[0] is False
    assert bucket.take("other")[0] is True


# ========================================================= heartbeats
def test_heartbeat_is_stored_with_server_receive_time(t):
    sid, started, inst = hexid(), iso(seconds=-30, hours=0), hexid()
    r = post(t, STATUS_URL, live(sid, started, 1, inst, unique=counts(car=2, person=1)), t.a)
    body = r.get_json()
    assert r.status_code == 200 and body["accepted"] is True and body["session"] == "live"
    assert body["heartbeat_timeout_s"] == 15 and body["server_time"].endswith("+00:00")
    s = stored_session(t, sid)
    assert s["status"] == "running" and s["unique"]["car"] == 2 and s["started_at"] == started
    assert s["first_received_at"].endswith("+00:00") and s["provenance"]["instance_id"] == inst


def test_desktop_timestamps_are_kept_as_sent(t):
    """A desktop offset is preserved verbatim; server times are separate UTC columns."""
    from datetime import datetime, timezone, timedelta as td

    plus2 = datetime.now(timezone(td(hours=2)))
    started = (plus2 - td(seconds=20)).isoformat(timespec="milliseconds")
    sid = hexid()
    assert post(t, STATUS_URL, live(sid, started, 1, hexid()), t.a).status_code == 200
    s = stored_session(t, sid)
    assert s["started_at"] == started and started.endswith("+02:00")
    assert s["first_received_at"].endswith("+00:00")


def test_stale_and_reordered_heartbeats_are_ignored(t):
    sid, started, inst = hexid(), iso(seconds=-30), hexid()
    assert post(t, STATUS_URL, live(sid, started, 5, inst, unique=counts(car=5)), t.a).get_json()["accepted"]
    for seq in (5, 4, 1):  # duplicate, then older ones arriving late
        r = post(t, STATUS_URL, live(sid, started, seq, inst, unique=counts(car=1)), t.a)
        assert r.status_code == 200 and r.get_json() == {"accepted": False, "reason": "stale"}
    assert stored_session(t, sid)["unique"]["car"] == 5
    assert post(t, STATUS_URL, live(sid, started, 6, inst, unique=counts(car=6)), t.a).get_json()["accepted"]
    assert stored_session(t, sid)["unique"]["car"] == 6


def test_instantaneous_values_may_decrease(t):
    sid, started, inst = hexid(), iso(seconds=-30), hexid()
    first = live(sid, started, 1, inst, visible=counts(car=9, person=4))
    first["performance"].update(frames_dropped=50, active_tracks=13, inference_fps=30.0)
    assert post(t, STATUS_URL, first, t.a).status_code == 200
    second = live(sid, started, 2, inst, visible=counts())
    second["performance"].update(frames_dropped=0, active_tracks=0, inference_fps=1.0)  # camera reconnected
    assert post(t, STATUS_URL, second, t.a).get_json()["accepted"] is True
    view = read(t, "/api/monitor/desktop/status").get_json()["devices"][0]
    assert view["visible"] == counts() and view["performance"]["frames_dropped"] == 0


@pytest.mark.parametrize("field,first,second", [
    ("unique", counts(car=3), counts(car=2)),
    ("crossings", counts(person=2), counts(person=1)),
    ("line_totals", lines(a_car=2), lines(a_car=1)),
    ("line_totals", lines(a_car=2), {}),  # a line's totals never disappear within a session
])
def test_cumulative_counters_may_not_decrease(t, field, first, second):
    sid, started, inst = hexid(), iso(seconds=-30), hexid()
    assert post(t, STATUS_URL, live(sid, started, 1, inst, **{field: first}), t.a).status_code == 200
    r = post(t, STATUS_URL, live(sid, started, 2, inst, **{field: second}), t.a)
    assert r.status_code == 422 and r.get_json()["code"] == "counter_decreased"
    assert stored_session(t, sid)[field if field != "line_totals" else "line_totals"] == first


def test_frames_processed_may_not_decrease(t):
    sid, started, inst = hexid(), iso(seconds=-30), hexid()
    assert post(t, STATUS_URL, live(sid, started, 1, inst, frames=100), t.a).status_code == 200
    assert post(t, STATUS_URL, live(sid, started, 2, inst, frames=99), t.a).status_code == 422


def test_rejected_heartbeat_changes_nothing(t):
    sid, started, inst = hexid(), iso(seconds=-30), hexid()
    post(t, STATUS_URL, live(sid, started, 1, inst, unique=counts(car=3)), t.a)
    bad = live(sid, started, 2, inst, unique=counts(car=1))
    bad["state"] = "Recording"
    assert post(t, STATUS_URL, bad, t.a).status_code == 422
    with t.app.app_context():
        row = DesktopStatus.query.one()
        assert row.seq == 1 and row.document["state"] == "Detecting"


def test_new_session_on_the_same_instance_after_reset(t):
    """Reset Counters closes a session and opens a new one; counters start from zero again."""
    inst, started = hexid(), iso(seconds=-60)
    s1, s2 = hexid(), hexid()
    post(t, STATUS_URL, live(s1, started, 1, inst, unique=counts(car=7)), t.a)
    r = post(t, STATUS_URL, live(s2, iso(seconds=-1), 2, inst, unique=counts()), t.a)
    assert r.get_json()["accepted"] is True
    assert stored_session(t, s1)["unique"]["car"] == 7 and stored_session(t, s2)["unique"]["car"] == 0


@pytest.mark.parametrize("field", ["sent_at", "instance_started_at"])
def test_future_timestamps_are_refused(t, field):
    doc = status(hexid(), 1, **{field: iso(seconds=3600)})
    if field == "instance_started_at":
        doc["sent_at"] = iso(seconds=3600)
    r = post(t, STATUS_URL, doc, t.a)
    assert r.status_code == 422 and r.get_json()["code"] == "clock_skew"


def test_old_heartbeat_is_refused(t):
    r = post(t, STATUS_URL, status(hexid(), 1, instance_started_at=iso(hours=-2), sent_at=iso(seconds=-3600)), t.a)
    assert r.status_code == 422 and r.get_json()["code"] == "clock_skew"


def test_session_changes_are_refused(t):
    sid, started, inst = hexid(), iso(seconds=-30), hexid()
    post(t, STATUS_URL, live(sid, started, 1, inst), t.a)
    moved = live(sid, iso(seconds=-20), 2, inst)
    assert post(t, STATUS_URL, moved, t.a).get_json()["code"] == "session_mismatch"
    other_source = live(sid, started, 3, inst)
    other_source["source"] = {"kind": "video_file", "label": "clip.mp4"}
    assert post(t, STATUS_URL, other_source, t.a).get_json()["code"] == "session_mismatch"


# =================================================== restart, reconnect
def test_desktop_restart_and_reconnection(t):
    started = iso(seconds=-120)
    a_inst, b_inst, s1, s2 = hexid(), hexid(), hexid(), hexid()
    a_start, b_start = iso(seconds=-120), iso(seconds=-5)
    # process A reports session S1, then crashes
    assert post(t, STATUS_URL, live(s1, started, 50, a_inst, unique=counts(car=4, person=2),
                                    crossings=counts(car=1), instance_started_at=a_start), t.a).status_code == 200
    # process B starts later: accepted even though its seq is lower
    r = post(t, STATUS_URL, status(b_inst, 1, instance_started_at=b_start), t.a)
    assert r.get_json()["accepted"] is True
    # a delayed heartbeat from A is refused while B is alive
    late = live(s1, started, 51, a_inst, unique=counts(car=9), instance_started_at=a_start)
    assert post(t, STATUS_URL, late, t.a).get_json() == {"accepted": False, "reason": "superseded"}
    assert stored_session(t, s1)["unique"]["car"] == 4
    # B may not continue A's session
    r = post(t, STATUS_URL, live(s1, started, 2, b_inst, instance_started_at=b_start), t.a)
    assert r.status_code == 409 and r.get_json()["code"] == "session_instance_mismatch"
    # S1 is shown as not live, B's new session is live
    assert post(t, STATUS_URL, live(s2, iso(seconds=-1), 3, b_inst, instance_started_at=b_start), t.a).status_code == 200
    sessions = {s["session_id"]: s for s in read(t, "/api/monitor/desktop/sessions").get_json()["sessions"]}
    assert sessions[s1]["live"] is False and sessions[s1]["status"] == "running"
    assert sessions[s2]["live"] is True
    # B uploads S1 as interrupted without statistics (A crashed before saving them)
    assert post(t, events_url(s1), events(s1, started, [event(7, timestamp=iso(seconds=-60))]), t.a).status_code == 200
    r = post(t, summary_url(s1), summary(s1, started, status="interrupted", stats_complete=False,
                                          crossings=counts(car=1), unique=counts(), frames=0), t.a)
    assert r.status_code == 201 and r.get_json()["counts_origin"] == "last_live_update"
    s = stored_session(t, s1)
    assert s["status"] == "interrupted" and s["unique"] == counts(car=4, person=2) and s["crossings"]["car"] == 1


def test_older_instance_is_accepted_again_once_the_newer_one_is_silent(t):
    a_inst, b_inst = hexid(), hexid()
    post(t, STATUS_URL, status(b_inst, 1, instance_started_at=iso(seconds=-5)), t.a)
    old = status(a_inst, 10, instance_started_at=iso(seconds=-100))
    assert post(t, STATUS_URL, old, t.a).get_json()["reason"] == "superseded"
    age_status(t, "laptop-a", 60)
    assert post(t, STATUS_URL, status(a_inst, 11, instance_started_at=iso(seconds=-100)), t.a).get_json()["accepted"]


def test_connection_states(t):
    def device():
        return read(t, "/api/monitor/desktop/status").get_json()["devices"][0]

    assert device()["desktop"]["connection"] == "never_connected" and device()["live"] is False
    doc = live(hexid(), iso(seconds=-10), 1, hexid(), unique=counts(car=3, person=2))
    doc["recording"] = {"active": True, "file": "atcs_20261023-120000_3f1c0d6e.mp4"}
    doc["state"] = "Recording"
    post(t, STATUS_URL, doc, t.a)
    d = device()
    assert d["desktop"]["connection"] == "connected" and d["live"] and d["running"] and d["recording"]["active"]
    assert d["totals"]["unique_vehicles"] == 3 and d["totals"]["unique_pedestrians"] == 2
    assert d["desktop"]["app"] == {"name": "ATCS Monitor", "version": "1.0.0"}
    age_status(t, "laptop-a", 20)
    d = device()
    assert d["desktop"]["connection"] == "disconnected" and not d["live"] and not d["running"]
    assert d["recording"]["active"] is False and d["desktop"]["seconds_since_seen"] >= 20


# ============================================================ events
def test_events_are_deduplicated(t):
    sid, started = hexid(), iso(seconds=-30)
    page = [event(1), event(2, cls="person", direction="b_to_a"), event(3, line_id="line-2")]
    r = post(t, events_url(sid), events(sid, started, page), t.a)
    assert r.status_code == 200 and r.get_json() == {"inserted": 3, "duplicates": 0, "stored_events": 3}
    r = post(t, events_url(sid), events(sid, started, page), t.a)
    assert r.get_json() == {"inserted": 0, "duplicates": 3, "stored_events": 3}
    overlap = page[1:] + [event(4), event(1, direction="b_to_a")]  # same track, other direction: a new event
    r = post(t, events_url(sid), events(sid, started, list(reversed(overlap))), t.a)
    assert r.get_json() == {"inserted": 2, "duplicates": 2, "stored_events": 5}


def test_conflicting_duplicate_event_is_refused(t):
    sid, started = hexid(), iso(seconds=-30)
    original = event(1, timestamp=iso(seconds=-10))
    post(t, events_url(sid), events(sid, started, [original]), t.a)
    changed = dict(original, **{"class": "truck"})
    r = post(t, events_url(sid), events(sid, started, [event(2), changed]), t.a)
    assert r.status_code == 409 and r.get_json()["code"] == "event_conflict"
    with t.app.app_context():
        assert DesktopCrossingEvent.query.count() == 1  # the whole page was rolled back


def test_events_before_session_start_are_refused(t):
    sid, started = hexid(), iso(seconds=-30)
    r = post(t, events_url(sid), events(sid, started, [event(1, timestamp=iso(seconds=-60))]), t.a)
    assert r.status_code == 422 and r.get_json()["code"] == "invalid_timestamp"


def test_events_must_match_the_session(t):
    sid, started, inst = hexid(), iso(seconds=-30), hexid()
    post(t, STATUS_URL, live(sid, started, 1, inst), t.a)
    r = post(t, events_url(sid), events(sid, started, [event(1)], source="video_file", source_id="clip.mp4"), t.a)
    assert r.status_code == 409 and r.get_json()["code"] == "session_mismatch"


# =========================================================== summary
def finalize(env, sid, started, evs, token=None, **kw):
    token = token or env.a
    if evs:
        assert post(env, events_url(sid), events(sid, started, evs), token).status_code == 200
    tally = counts()
    for e in evs:
        tally[e["class"]] += 1
    return post(env, summary_url(sid), summary(sid, started, crossings=tally, **kw), token)


def test_summary_is_idempotent(t):
    sid, started = hexid(), iso(seconds=-60)
    post(t, STATUS_URL, live(sid, started, 1, hexid(), unique=counts(car=1)), t.a)
    evs = [event(1), event(2, cls="person")]
    post(t, events_url(sid), events(sid, started, evs), t.a)
    doc = summary(sid, started, crossings=counts(car=1, person=1), unique=counts(car=2, person=1))
    r1 = post(t, summary_url(sid), doc, t.a)
    assert r1.status_code == 201 and r1.get_json()["duplicate"] is False and r1.get_json()["counts_origin"] == "summary"
    r2 = post(t, summary_url(sid), doc, t.a)
    assert r2.status_code == 200 and r2.get_json()["duplicate"] is True
    reordered = json.loads(json.dumps(dict(reversed(list(doc.items())))))
    assert post(t, summary_url(sid), reordered, t.a).status_code == 200
    s = stored_session(t, sid)
    assert s["finalized"] and s["status"] == "completed" and s["crossing_event_count"] == 2
    assert s["unique"] == counts(car=2, person=1) and s["recordings"][0]["file"].endswith(".mp4")


def test_conflicting_summary_is_refused_and_original_kept(t):
    sid, started = hexid(), iso(seconds=-60)
    assert finalize(t, sid, started, [], unique=counts(car=2)).status_code == 201
    for change in ({"unique": counts(car=3)}, {"status": "error"}, {"recordings": []}):
        doc = summary(sid, started, **change)
        r = post(t, summary_url(sid), doc, t.a)
        assert r.status_code == 409 and r.get_json()["code"] == "summary_conflict"
    s = stored_session(t, sid)
    assert s["unique"] == counts(car=2) and s["status"] == "completed" and len(s["recordings"]) == 1


def test_summary_requires_all_events(t):
    sid, started = hexid(), iso(seconds=-60)
    post(t, events_url(sid), events(sid, started, [event(1)]), t.a)
    r = post(t, summary_url(sid), summary(sid, started, crossings=counts(car=2)), t.a)
    assert r.status_code == 409 and r.get_json() == {
        "error": "upload all crossing events of this session before its summary", "code": "events_incomplete",
        "stored_events": 1}
    # same count, other classes
    r = post(t, summary_url(sid), summary(sid, started, crossings=counts(person=1)), t.a)
    assert r.status_code == 409
    post(t, events_url(sid), events(sid, started, [event(2)]), t.a)
    assert post(t, summary_url(sid), summary(sid, started, crossings=counts(car=2)), t.a).status_code == 201


def test_summary_without_events_for_an_unseen_session(t):
    sid, started = hexid(), iso(seconds=-60)
    r = post(t, summary_url(sid), summary(sid, started), t.a)
    assert r.status_code == 201
    s = stored_session(t, sid)
    assert s["status"] == "completed" and s["counts_origin"] == "summary" and s["provenance"]["instance_id"] is None
    r = post(t, summary_url(hexid()), summary(hexid(), started, crossings=counts(car=1)), t.a)
    assert r.status_code == 400  # session id differs from URL
    sid2 = hexid()
    r = post(t, summary_url(sid2), summary(sid2, started, crossings=counts(car=1)), t.a)
    assert r.status_code == 409 and r.get_json()["stored_events"] == 0
    assert stored_session(t, sid2) is None


def test_offline_session_upload(t):
    """The desktop was not connected during the session and uploads it afterwards."""
    sid, started = hexid(), iso(seconds=-600)
    evs = [event(i, timestamp=iso(seconds=-500 + i), cls=("car", "person", "bus")[i % 3]) for i in range(1, 8)]
    for chunk in (evs[:3], evs[3:]):
        assert post(t, events_url(sid), events(sid, started, chunk), t.a).status_code == 200
    assert stored_session(t, sid)["status"] == "running"
    r = post(t, summary_url(sid), summary(sid, started, crossings=counts(car=2, person=3, bus=2)), t.a)
    assert r.status_code == 201
    listed = read(t, "/api/monitor/desktop/sessions").get_json()["sessions"][0]
    assert listed["status"] == "completed" and listed["live"] is False
    assert listed["totals"] == {"unique_vehicles": 1, "crossing_vehicles": 4, "unique_pedestrians": 0,
                                "crossing_pedestrians": 3}


def test_summary_lower_than_live_counts_is_refused(t):
    sid, started = hexid(), iso(seconds=-60)
    post(t, STATUS_URL, live(sid, started, 1, hexid(), unique=counts(car=5), frames=200), t.a)
    r = post(t, summary_url(sid), summary(sid, started, unique=counts(car=4), frames=300), t.a)
    assert r.status_code == 422 and r.get_json()["code"] == "counter_decreased"
    assert not stored_session(t, sid)["finalized"]


def test_incomplete_summary_without_live_data(t):
    sid, started = hexid(), iso(seconds=-60)
    r = post(t, summary_url(sid), summary(sid, started, status="interrupted", stats_complete=False), t.a)
    assert r.status_code == 201 and r.get_json()["counts_origin"] == "incomplete"


def test_finalized_session_ignores_late_heartbeats_and_new_events(t):
    sid, started, inst = hexid(), iso(seconds=-60), hexid()
    post(t, STATUS_URL, live(sid, started, 1, inst, unique=counts(car=1)), t.a)
    first = event(1)
    assert finalize(t, sid, started, [first], unique=counts(car=1)).status_code == 201
    r = post(t, STATUS_URL, live(sid, started, 2, inst, unique=counts(car=9)), t.a)
    assert r.get_json()["accepted"] is True and r.get_json()["session"] == "finalized"
    assert stored_session(t, sid)["unique"]["car"] == 1
    assert post(t, events_url(sid), events(sid, started, [first]), t.a).status_code == 200  # replay is fine
    r = post(t, events_url(sid), events(sid, started, [event(2)]), t.a)
    assert r.status_code == 409 and r.get_json()["code"] == "session_finalized"


def test_summary_paths_are_refused(t):
    sid, started = hexid(), iso(seconds=-60)
    doc = summary(sid, started, recordings=[{"file": "C:\\Users\\alber\\a.mp4", "ok": True, "duration_s": 1.0,
                                              "codec": "avc1"}])
    assert post(t, summary_url(sid), doc, t.a).status_code == 400
    doc = summary(sid, started, source="video_file", source_id="/home/alberto/clip.mp4")
    assert post(t, summary_url(sid), doc, t.a).status_code == 400
    assert stored_session(t, sid) is None


# ================================================== multiple devices
def test_devices_cannot_touch_each_others_sessions(t):
    sid, started = hexid(), iso(seconds=-60)
    post(t, STATUS_URL, live(sid, started, 1, hexid(), unique=counts(car=2)), t.a)
    post(t, events_url(sid), events(sid, started, [event(1)]), t.a)
    for url, doc in ((STATUS_URL, live(sid, started, 1, hexid(), unique=counts(car=50))),
                     (events_url(sid), events(sid, started, [event(2)])),
                     (summary_url(sid), summary(sid, started, crossings=counts(car=1)))):
        r = post(t, url, doc, t.b)
        assert r.status_code == 409 and r.get_json()["code"] == "session_conflict"
    s = stored_session(t, sid)
    assert s["device"] == "laptop-a" and s["unique"]["car"] == 2 and not s["finalized"]
    with t.app.app_context():
        assert DesktopCrossingEvent.query.count() == 1
        assert DesktopStatus.query.count() == 1  # B's refused heartbeat stored nothing


def test_multiple_devices_are_isolated(t):
    inst = hexid()  # even the same instance id on two devices does not mix them
    sa, sb, started = hexid(), hexid(), iso(seconds=-30)
    post(t, STATUS_URL, live(sa, started, 10, inst, unique=counts(car=1)), t.a)
    assert post(t, STATUS_URL, live(sb, started, 1, inst, unique=counts(person=4)), t.b).get_json()["accepted"]
    devices = {d["desktop"]["name"]: d for d in read(t, "/api/monitor/desktop/status").get_json()["devices"]}
    assert devices["laptop-a"]["session_id"] == sa and devices["laptop-b"]["session_id"] == sb
    assert devices["laptop-a"]["unique"]["car"] == 1 and devices["laptop-b"]["unique"]["person"] == 4
    only_b = read(t, "/api/monitor/desktop/sessions?device=laptop-b").get_json()["sessions"]
    assert [s["session_id"] for s in only_b] == [sb] and only_b[0]["device"] == "laptop-b"


# ======================================================= persistence
def test_data_survives_a_server_restart(tmp_path):
    class FileConfig(ConfigForTesting):
        SQLALCHEMY_DATABASE_URI = "sqlite:///" + str(tmp_path / "atcs.db")

    env = make_app(FileConfig)
    sid, started = hexid(), iso(seconds=-60)
    post(env, STATUS_URL, live(sid, started, 1, hexid(), unique=counts(car=1)), env.a)
    evs = [event(1), event(2)]
    assert finalize(env, sid, started, evs, unique=counts(car=2)).status_code == 201
    with env.app.app_context():
        db.session.remove()
        db.engine.dispose()

    again = create_app(FileConfig)  # a new process on the same database
    env2 = SimpleNamespace(app=again, client=again.test_client(), a=env.a, b=env.b)
    s = read(env2, f"/api/monitor/desktop/sessions/{sid}").get_json()
    assert s["status"] == "completed" and s["unique"]["car"] == 2 and s["crossing_event_count"] == 2
    assert s["crossings"]["car"] == 2 and s["started_at"] == started
    assert post(env2, summary_url(sid), summary(sid, started, crossings=counts(car=2), unique=counts(car=2)),
                env2.a).status_code in (200, 409)  # never a second finalization
    assert post(env2, events_url(sid), events(sid, started, evs), env2.a).get_json()["duplicates"] == 2
    with again.app_context():
        assert DesktopSession.query.count() == 1 and DesktopCrossingEvent.query.count() == 2
        db.session.remove()
        db.engine.dispose()


# ====================================================== browser reads
@pytest.mark.parametrize("url", READ_URLS + ["/api/monitor/desktop/sessions/" + "a" * 32])
def test_read_endpoints_need_a_grant(t, url):
    assert t.client.get(url).status_code == 401
    assert t.client.get(url, headers=jwt_headers(t, "stranger")).status_code == 403
    expired = jwt_headers(t, "view_user", expires_delta=timedelta(seconds=-1))
    r = t.client.get(url, headers=expired)
    assert r.status_code == 401 and r.get_json() == {"error": "token expired"}
    for user in ("view_user", "op_user"):
        assert t.client.get(url, headers=jwt_headers(t, user)).status_code in (200, 404)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_read_endpoints_are_read_only(t, method):
    for url in READ_URLS:
        r = t.client.open(url, method=method, headers=jwt_headers(t, "op_user"), json={})
        assert r.status_code == 405


@pytest.mark.parametrize("query", ["limit=0", "limit=201", "limit=abc", "limit=-1", "device=../x", "foo=1",
                                   "limit=%D9%A3"])
def test_session_list_query_is_validated(t, query):
    r = read(t, "/api/monitor/desktop/sessions?" + query)
    assert r.status_code == 400 and r.get_json()["code"] == "invalid_query"


def test_session_list_order_and_limit(t):
    ids = []
    for i in range(5):
        sid = hexid()
        ids.append(sid)
        post(t, summary_url(sid), summary(sid, iso(seconds=-1000 + i * 100)), t.a)
    listed = read(t, "/api/monitor/desktop/sessions?limit=3").get_json()["sessions"]
    assert [s["session_id"] for s in listed] == list(reversed(ids))[:3]
    for s in listed:
        assert "summary_json" not in s and "line_totals" not in s


def test_session_detail(t):
    sid, started = hexid(), iso(seconds=-60)
    finalize(t, sid, started, [event(1)], line_totals=lines(a_car=1))
    s = read(t, f"/api/monitor/desktop/sessions/{sid}").get_json()
    assert s["line_totals"] == lines(a_car=1) and s["model"]["name"] == "yolo26n.pt"
    assert s["provenance"]["device"] == "laptop-a" and s["recordings"][0]["file"].endswith(".mp4")
    assert read(t, f"/api/monitor/desktop/sessions/{hexid()}").status_code == 404
    assert read(t, "/api/monitor/desktop/sessions/NOT-HEX").status_code == 400


def test_status_view_has_phase4_field_names(t):
    post(t, STATUS_URL, live(hexid(), iso(seconds=-10), 1, hexid()), t.a)
    d = read(t, "/api/monitor/desktop/status").get_json()["devices"][0]
    for key in ("running", "state", "message", "source", "session_id", "recording", "classes", "vehicle_classes",
                "visible", "unique", "crossings", "line_totals", "totals", "performance", "device", "last_error"):
        assert key in d
    assert set(d["performance"]) >= {"capture_fps", "inference_fps", "latency_ms", "frames_processed",
                                     "frames_dropped", "active_tracks"}


# ============================================================ secrecy
def test_device_tokens_never_reach_logs_or_responses(t, caplog):
    caplog.set_level(logging.DEBUG)
    sid, started = hexid(), iso(seconds=-60)
    responses = [
        post(t, STATUS_URL, live(sid, started, 1, hexid()), t.a),
        post(t, STATUS_URL, live(sid, started, 2, hexid()), t.b),  # conflict, logged
        post(t, STATUS_URL, status(hexid(), 1), t.a + "x"),
        post(t, STATUS_URL, status(hexid(), 1), t.a, environ_base={"REMOTE_ADDR": "10.1.1.1"}),
        read(t, "/api/monitor/desktop/status"),
        read(t, "/api/monitor/desktop/sessions"),
    ]
    for r in responses:
        for token in (t.a, t.b):
            assert token.encode() not in r.get_data() and token not in str(r.headers)
            assert hash_token(token).encode() not in r.get_data()
    for token in (t.a, t.b):
        assert token not in caplog.text and hash_token(token) not in caplog.text
    assert "rejected" in caplog.text


def test_existing_monitor_endpoints_unchanged(t):
    """Phase 3/4 endpoints still answer as before."""
    t.app.config["MONITOR_SERVICE_SECRET"] = None
    r = t.client.get("/api/monitor/status", headers=jwt_headers(t, "view_user"))
    assert r.status_code == 503 and r.get_json()["code"] == "service_not_configured"
    assert t.client.get("/api/monitor/access", headers=jwt_headers(t, "op_user")).get_json() == {
        "role": "operator", "can_view": True, "can_control": True}
    assert t.client.get("/api/health").get_json() == {"status": "ok"}
