from datetime import datetime, timedelta

import pytest

from app import db
from app.models import Run

T0 = datetime(2026, 10, 1, 12, 0, 0)


def add_run(app, **fields):
    values = {
        "scenario_id": 1,
        "controller": "fixed",
        "status": "completed",
        "created_at": T0,
        "vehicles_completed": 1400,
        "avg_wait_s": 10.0,
        "max_wait_s": 40.0,
        "avg_time_loss_s": 20.0,
        "avg_trip_duration_s": 50.0,
    }
    values.update(fields)
    with app.app_context():
        run = Run(**values)
        db.session.add(run)
        db.session.commit()
        return run.id


def compare(client, headers, scenario_id=1):
    return client.get(f"/api/compare?scenario_id={scenario_id}", headers=headers)


def by_controller(data):
    return {c["controller"]: c for c in data["controllers"]}


def test_valid_comparison(client, app, auth_headers):
    fixed = add_run(app)
    queue = add_run(app, controller="queue_based", avg_wait_s=6.0, max_wait_s=25.0,
                    avg_time_loss_s=15.0, avg_trip_duration_s=45.0, vehicles_completed=1398)
    response = compare(client, auth_headers)
    data = response.get_json()
    assert response.status_code == 200
    assert data["baseline"] == "fixed"
    assert data["scenario"]["id"] == 1
    assert data["scenario"]["name"] == "uneven"
    rows = by_controller(data)
    assert rows["fixed"]["run_id"] == fixed
    assert rows["queue_based"]["run_id"] == queue
    assert rows["queue_based"]["vehicles_completed"] == 1398
    assert rows["queue_based"]["avg_wait_s"] == 6.0
    assert rows["queue_based"]["improvement_vs_fixed_pct"] == {
        "avg_wait_s": 40.0,
        "max_wait_s": 37.5,
        "avg_time_loss_s": 25.0,
        "avg_trip_duration_s": 10.0,
    }
    assert set(rows["fixed"]["improvement_vs_fixed_pct"].values()) == {0.0}
    assert "vehicles_completed" not in rows["fixed"]["improvement_vs_fixed_pct"]


def test_entry_fields(client, app, auth_headers):
    add_run(app)
    entry = compare(client, auth_headers).get_json()["controllers"][0]
    assert set(entry) == {
        "controller", "run_id", "vehicles_completed", "avg_wait_s", "max_wait_s",
        "avg_time_loss_s", "avg_trip_duration_s", "improvement_vs_fixed_pct",
    }


def test_multiple_controllers_in_configured_order(client, app, auth_headers):
    add_run(app, controller="wait_time")
    add_run(app, controller="queue_based")
    add_run(app, controller="fixed")
    names = [c["controller"] for c in compare(client, auth_headers).get_json()["controllers"]]
    assert names == ["fixed", "queue_based", "wait_time"]


def test_regression_is_negative(client, app, auth_headers):
    add_run(app)
    add_run(app, controller="wait_time", avg_time_loss_s=25.0)
    pct = by_controller(compare(client, auth_headers).get_json())["wait_time"]["improvement_vs_fixed_pct"]
    assert pct["avg_time_loss_s"] == -25.0


def test_rounding_to_one_decimal(client, app, auth_headers):
    add_run(app, avg_wait_s=3.0)
    add_run(app, controller="queue_based", avg_wait_s=2.0)  # 33.333...%
    add_run(app, controller="wait_time", avg_wait_s=1.0 - 1e-9)  # 66.666...%
    rows = by_controller(compare(client, auth_headers).get_json())
    assert rows["queue_based"]["improvement_vs_fixed_pct"]["avg_wait_s"] == 33.3
    assert rows["wait_time"]["improvement_vs_fixed_pct"]["avg_wait_s"] == 66.7


def test_latest_completed_run_selected(client, app, auth_headers):
    add_run(app, avg_wait_s=10.0, created_at=T0)
    newer = add_run(app, avg_wait_s=8.0, created_at=T0 + timedelta(minutes=5))
    add_run(app, avg_wait_s=9.0, created_at=T0 + timedelta(minutes=1))
    row = by_controller(compare(client, auth_headers).get_json())["fixed"]
    assert row["run_id"] == newer
    assert row["avg_wait_s"] == 8.0


def test_same_timestamp_breaks_tie_by_id(client, app, auth_headers):
    add_run(app, created_at=T0)
    second = add_run(app, created_at=T0)
    assert by_controller(compare(client, auth_headers).get_json())["fixed"]["run_id"] == second


@pytest.mark.parametrize("status", ["pending", "running", "failed"])
def test_unfinished_runs_excluded(client, app, auth_headers, status):
    done = add_run(app, created_at=T0)
    add_run(app, status=status, created_at=T0 + timedelta(hours=1), avg_wait_s=None)
    add_run(app, controller="wait_time", status=status, created_at=T0 + timedelta(hours=1))
    data = compare(client, auth_headers).get_json()
    assert [c["controller"] for c in data["controllers"]] == ["fixed"]
    assert data["controllers"][0]["run_id"] == done


def test_other_scenarios_ignored(client, app, auth_headers):
    other = client.post(
        "/api/scenarios", json={"name": "other", "ew_vehs_per_hour": 100, "ns_vehs_per_hour": 100}
    ).get_json()["id"]
    add_run(app, scenario_id=other, controller="queue_based")
    assert compare(client, auth_headers).get_json()["controllers"] == []
    assert len(compare(client, auth_headers, other).get_json()["controllers"]) == 1


def test_missing_fixed_baseline_gives_null_improvement(client, app, auth_headers):
    add_run(app, controller="queue_based")
    add_run(app, controller="wait_time")
    add_run(app, controller="fixed", status="failed")
    controllers = compare(client, auth_headers).get_json()["controllers"]
    assert len(controllers) == 2
    assert all(c["improvement_vs_fixed_pct"] is None for c in controllers)
    assert controllers[0]["avg_wait_s"] == 10.0


def test_zero_baseline_metric_is_null(client, app, auth_headers):
    add_run(app, avg_wait_s=0.0)
    add_run(app, controller="queue_based", avg_wait_s=0.0)
    add_run(app, controller="wait_time", avg_wait_s=2.0)
    rows = by_controller(compare(client, auth_headers).get_json())
    for name in ("fixed", "queue_based", "wait_time"):
        pct = rows[name]["improvement_vs_fixed_pct"]
        assert pct["avg_wait_s"] is None
        assert pct["avg_time_loss_s"] is not None


def test_missing_metric_stays_null(client, app, auth_headers):
    add_run(app)
    add_run(app, controller="queue_based", max_wait_s=None)
    row = by_controller(compare(client, auth_headers).get_json())["queue_based"]
    assert row["max_wait_s"] is None
    assert row["improvement_vs_fixed_pct"]["max_wait_s"] is None
    assert row["improvement_vs_fixed_pct"]["avg_wait_s"] == 0.0


def test_no_completed_runs(client, auth_headers):
    response = compare(client, auth_headers)
    assert response.status_code == 200
    data = response.get_json()
    assert data["controllers"] == []
    assert data["scenario"]["id"] == 1


def test_missing_scenario_id(client, auth_headers):
    response = client.get("/api/compare", headers=auth_headers)
    assert response.status_code == 400
    assert response.get_json() == {"error": "scenario_id is required"}


@pytest.mark.parametrize("value", ["", "abc", "1.5", "-1", "1e3", " 1", "%C2%B2"])
def test_invalid_scenario_id(client, auth_headers, value):
    response = client.get(f"/api/compare?scenario_id={value}", headers=auth_headers)
    assert response.status_code == 400


def test_unknown_scenario(client, auth_headers):
    response = compare(client, auth_headers, 999)
    assert response.status_code == 404
    assert response.get_json() == {"error": "scenario not found"}


def test_requires_auth(client, app):
    add_run(app)
    assert client.get("/api/compare?scenario_id=1").status_code == 401
    assert client.get("/api/compare").status_code == 401
