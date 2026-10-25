"""The Monitor page's test payloads still match the read-only desktop endpoints.

frontend/tests/fixtures/desktop-payloads.json is served by the Playwright mock
server. If a read view changes shape, this fails until the fixture is
regenerated (python -m tests.desktop_dashboard_fixture) and the page checked.
"""

import json

from tests.desktop_dashboard_fixture import FIXTURE, build


def shape(value):
    """Field names and JSON types, recursively; values are ignored."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(v) for v in value[:1]]
    if isinstance(value, bool) or value is None:
        return type(value).__name__
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def test_fixture_has_the_shape_the_backend_returns():
    stored = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fresh = build()
    assert sorted(stored) == sorted(fresh)
    for name in fresh:
        assert shape(stored[name]) == shape(fresh[name]), name


def test_fixture_covers_the_states_the_page_distinguishes():
    p = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert p["device_never"]["desktop"]["connection"] == "never_connected" and p["device_never"]["state"] is None
    assert p["device_idle"]["live"] and p["device_idle"]["session_id"] is None
    rec = p["device_recording"]
    assert rec["running"] and rec["recording"]["active"] and rec["state"] == "Recording"
    assert "/" not in rec["recording"]["file"] and "\\" not in rec["recording"]["file"]
    assert p["session_completed"]["counts_origin"] == "summary"
    assert p["session_interrupted"]["counts_origin"] == "incomplete"
    assert p["session_error"]["counts_origin"] == "last_live_update"
    assert p["session_running"]["live"] and not p["session_running"]["finalized"]
    assert p["detail_completed"]["recordings"] and isinstance(p["session_completed"]["recordings"], int)
