"""Real /api/monitor/desktop/* responses for the frontend tests.

Builds a small history through the telemetry endpoints of a test app (live
session with recording, idle device, completed, interrupted, error and still
running sessions) and returns what the read-only browser endpoints answer.
frontend/tests/fixtures/desktop-payloads.json is this output; the Playwright
mock server serves copies of it, and test_desktop_dashboard_contract.py checks
that the file still has the shape the backend returns.

Regenerate after a change to the read views:

    cd backend && venv/bin/python -m tests.desktop_dashboard_fixture
"""

import json
from pathlib import Path

from app import db
from app.services import monitor_devices
from tests.desktop_docs import counts, event, events, hexid, iso, lines, session_block, status, summary
from tests.test_desktop_telemetry import STATUS_URL, events_url, make_app, post, read, summary_url

FIXTURE = Path(__file__).resolve().parents[2] / "frontend" / "tests" / "fixtures" / "desktop-payloads.json"


def unknown_stats(doc):
    """As the desktop sends a session whose statistics were never saved (monitor/app/desktop_link/documents.py)."""
    return {**doc, "duration_s": 0.0, "avg_inference_fps": None}


def build():
    env = make_app()  # also registers laptop-a and laptop-b; they are revoked below
    with env.app.app_context():
        for name in ("laptop-a", "laptop-b"):
            monitor_devices.revoke(name)
        tokens = {name: monitor_devices.add(name) for name in ("alberto-windows", "lab-pc", "spare-laptop")}

    def get(url):
        r = read(env, url)
        assert r.status_code == 200, (url, r.status_code, r.get_json())
        return r.get_json()

    def send(url, doc, device):
        r = post(env, url, doc, tokens[device])
        assert r.status_code in (200, 201), (url, r.status_code, r.get_json())

    # A completed video session with a recording on alberto-windows.
    done_id, done_start = hexid(), iso(minutes=-30)
    done_crossings = counts(person=2, car=9, motorcycle=1, bicycle=1, bus=1, truck=2)
    send(events_url(done_id), events(done_id, done_start, [
        event(i, cls=c, timestamp=iso(minutes=-29)) for i, c in enumerate(
            [k for k, n in done_crossings.items() for _ in range(n)])], "video_file", "car-detection.mp4"),
        "alberto-windows")
    send(summary_url(done_id), summary(
        done_id, done_start, crossings=done_crossings, source="video_file", source_id="car-detection.mp4",
        unique=counts(person=7, car=20, motorcycle=1, bicycle=2, bus=2, truck=3), frames=1830,
        line_totals=lines(a_car=5, b_person=2)), "alberto-windows")

    # A session that crashed before saving statistics (interrupted, nothing live).
    crash_id = hexid()
    send(summary_url(crash_id), unknown_stats(summary(crash_id, iso(hours=-3), status="interrupted",
                                                      stats_complete=False, unique=counts(), frames=0,
                                                      recordings=[])), "lab-pc")

    # An error session that was seen live first: the backend keeps the last live counts.
    err_id, err_start, err_inst = hexid(), iso(hours=-2), hexid()
    send(STATUS_URL, status(err_inst, 1, instance_started_at=iso(hours=-2, minutes=-1),
                            session=session_block(err_id, err_start, unique=counts(car=4, person=1),
                                                  crossings=counts(), frames=240, timestamp=iso(hours=-2,
                                                                                                minutes=5))),
         "lab-pc")
    send(summary_url(err_id), unknown_stats(summary(err_id, err_start, status="error", stats_complete=False,
                                                    unique=counts(), frames=0, recordings=[])), "lab-pc")
    # lab-pc is idle now.
    send(STATUS_URL, status(hexid(), 1), "lab-pc")

    # alberto-windows is monitoring a camera and recording right now.
    live_id, live_start, live_inst = hexid(), iso(minutes=-2), hexid()
    send(STATUS_URL, status(live_inst, 7, state="Recording",
                            recording={"active": True, "file": "atcs_20261024-191500_5d2e9a41.mp4"},
                            session=session_block(live_id, live_start,
                                                  visible=counts(person=1, car=3, bus=1),
                                                  unique=counts(person=4, car=11, motorcycle=1, bicycle=1, bus=1,
                                                                truck=2),
                                                  crossings=counts(person=1, car=6, truck=1),
                                                  line_totals=lines(a_car=4, b_person=1), frames=1500)),
         "alberto-windows")

    devices = {d["desktop"]["name"]: d for d in get("/api/monitor/desktop/status")["devices"]}
    status_doc = get("/api/monitor/desktop/status")
    sessions = get("/api/monitor/desktop/sessions")["sessions"]
    by_status = {s["status"]: s for s in sessions}
    out = {
        "heartbeat_timeout_s": status_doc["heartbeat_timeout_s"],
        "device_recording": devices["alberto-windows"],
        "device_idle": devices["lab-pc"],
        "device_never": devices["spare-laptop"],
        "session_running": by_status["running"],
        "session_completed": by_status["completed"],
        "session_interrupted": by_status["interrupted"],
        "session_error": by_status["error"],
        "detail_completed": get(f"/api/monitor/desktop/sessions/{done_id}"),
        "detail_interrupted": get(f"/api/monitor/desktop/sessions/{crash_id}"),
        "detail_error": get(f"/api/monitor/desktop/sessions/{err_id}"),
        "detail_running": get(f"/api/monitor/desktop/sessions/{live_id}"),
    }
    with env.app.app_context():
        db.session.remove()
        db.drop_all()
    return out


def dump(payloads):
    return json.dumps(payloads, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(dump(build()), encoding="utf-8")
    print(f"wrote {FIXTURE}")
