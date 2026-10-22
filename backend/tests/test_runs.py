from unittest.mock import patch

from app import db
from app.models import Run


def fake_success(run):
    run.status = "completed"
    run.vehicles_completed = 1400
    run.avg_wait_s = 6.07
    run.max_wait_s = 25.0
    run.avg_time_loss_s = 15.11
    run.avg_trip_duration_s = 44.22
    db.session.commit()
    return None


def fake_failure(run):
    run.status = "failed"
    db.session.commit()
    return "simulation crashed"


def add_run(app, **fields):
    values = {"scenario_id": 1, "controller": "fixed", "status": "completed"}
    values.update(fields)
    with app.app_context():
        run = Run(**values)
        db.session.add(run)
        db.session.commit()
        return run.id


def test_list_controllers(client):
    response = client.get("/api/controllers")
    assert response.status_code == 200
    controllers = response.get_json()["controllers"]
    assert "fixed" in controllers
    assert "queue_based" in controllers


def test_create_run_success(client):
    with patch("app.api.runs.execute_run", side_effect=fake_success):
        response = client.post("/api/runs", json={"scenario_id": 1, "controller": "queue_based"})
    data = response.get_json()
    assert response.status_code == 201
    assert data["status"] == "completed"
    assert data["controller"] == "queue_based"
    assert data["scenario_id"] == 1
    assert data["avg_wait_s"] == 6.07
    assert data["vehicles_completed"] == 1400


def test_create_run_failure(client):
    with patch("app.api.runs.execute_run", side_effect=fake_failure):
        response = client.post("/api/runs", json={"scenario_id": 1, "controller": "fixed"})
    data = response.get_json()
    assert response.status_code == 500
    assert data["error"] == "simulation crashed"
    assert data["run"]["status"] == "failed"


def test_create_run_unknown_scenario(client):
    response = client.post("/api/runs", json={"scenario_id": 999, "controller": "fixed"})
    assert response.status_code == 404


def test_create_run_scenario_id_must_be_integer(client):
    response = client.post("/api/runs", json={"scenario_id": "1", "controller": "fixed"})
    assert response.status_code == 404


def test_create_run_unknown_controller(client):
    response = client.post("/api/runs", json={"scenario_id": 1, "controller": "magic"})
    data = response.get_json()
    assert response.status_code == 400
    assert data["error"] == "unknown controller"
    assert "fixed" in data["allowed"]


def test_create_run_without_body(client):
    response = client.post("/api/runs")
    assert response.status_code == 404


def test_get_run(client, app):
    run_id = add_run(app, avg_wait_s=4.5)
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.get_json()["avg_wait_s"] == 4.5


def test_get_missing_run(client):
    response = client.get("/api/runs/999")
    assert response.status_code == 404
    assert response.get_json() == {"error": "run not found"}


def test_list_runs_filters(client, app):
    other = client.post(
        "/api/scenarios",
        json={"name": "other", "ew_vehs_per_hour": 100, "ns_vehs_per_hour": 100},
    ).get_json()["id"]
    add_run(app, scenario_id=1, controller="fixed")
    add_run(app, scenario_id=1, controller="queue_based")
    add_run(app, scenario_id=other, controller="fixed")

    assert len(client.get("/api/runs").get_json()) == 3
    assert len(client.get("/api/runs?scenario_id=1").get_json()) == 2
    assert len(client.get(f"/api/runs?scenario_id={other}").get_json()) == 1
    assert len(client.get("/api/runs?controller=fixed").get_json()) == 2
    assert len(client.get("/api/runs?scenario_id=1&controller=queue_based").get_json()) == 1
    assert client.get("/api/runs?controller=wait_time").get_json() == []


def test_list_runs_ordered_by_id(client, app):
    first = add_run(app)
    second = add_run(app, controller="queue_based")
    ids = [r["id"] for r in client.get("/api/runs").get_json()]
    assert ids == [first, second]
