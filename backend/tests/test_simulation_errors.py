import logging
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.simulation import SIMULATION_ERROR

SECRET_PATH = "/home/someone/secret/simulation"
STDERR = f'Traceback (most recent call last):\n  File "{SECRET_PATH}/runner.py", line 1\nFatalTraCIError: boom'
STATS = {
    "vehicles_completed": 1400,
    "avg_wait_s": 6.18,
    "max_wait_s": 25.0,
    "avg_time_loss_s": 15.35,
    "avg_trip_duration_s": 44.0,
}

FAILURES = {
    "process_error": subprocess.CalledProcessError(
        1, [sys.executable, f"{SECRET_PATH}/runner.py", "fixed"], stderr=STDERR
    ),
    "timeout": subprocess.TimeoutExpired([sys.executable, f"{SECRET_PATH}/runner.py"], 300, stderr=STDERR),
    "missing_output": FileNotFoundError(2, "No such file or directory", f"{SECRET_PATH}/output/tripinfo_run_1.xml"),
}


def fake_metrics(summarize):
    return patch.dict(sys.modules, {"metrics": SimpleNamespace(summarize=summarize)})


def post_run(client, controller="fixed"):
    return client.post("/api/runs", json={"scenario_id": 1, "controller": controller})


def assert_no_internals(text, app):
    for leak in (SECRET_PATH, app.config["SIMULATION_DIR"], sys.executable, "Traceback", "runner.py", "tripinfo"):
        assert leak not in text


def test_successful_run_through_real_service(client):
    with patch("app.services.simulation.subprocess.run") as run, fake_metrics(lambda path: STATS):
        response = post_run(client, "wait_time")
    data = response.get_json()
    assert response.status_code == 201
    assert data["status"] == "completed"
    assert data["controller"] == "wait_time"
    assert data["avg_time_loss_s"] == 15.35
    assert data["vehicles_completed"] == 1400
    assert "error" not in data
    assert run.call_args.args[0][2] == "wait_time"


@pytest.mark.parametrize("kind", sorted(FAILURES))
def test_failure_returns_sanitized_error(client, app, kind):
    exc = FAILURES[kind]
    subprocess_effect = exc if kind != "missing_output" else None

    def summarize(path):
        raise exc

    with patch("app.services.simulation.subprocess.run", side_effect=subprocess_effect), fake_metrics(summarize):
        response = post_run(client)
    data = response.get_json()
    assert response.status_code == 500
    assert set(data) == {"error", "run"}
    assert data["error"] == SIMULATION_ERROR
    assert data["run"]["status"] == "failed"
    assert data["run"]["avg_wait_s"] is None
    assert_no_internals(response.get_data(as_text=True), app)


def test_failure_details_are_logged(client, caplog):
    with caplog.at_level(logging.ERROR), patch(
        "app.services.simulation.subprocess.run", side_effect=FAILURES["process_error"]
    ), fake_metrics(lambda path: STATS):
        run_id = post_run(client).get_json()["run"]["id"]
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(records) == 1
    record = records[0]
    assert f"simulation run {run_id} failed" in record.getMessage()
    assert "FatalTraCIError: boom" in record.getMessage()
    assert record.exc_info[0] is subprocess.CalledProcessError
    assert "CalledProcessError" in caplog.text


def test_metrics_import_failure_marks_run_failed(client, app):
    # previously raised outside the handler and left the run stuck in "running"
    with patch("app.services.simulation.subprocess.run"), patch.dict(sys.modules, {"metrics": None}):
        response = post_run(client)
    assert response.status_code == 500
    assert response.get_json()["error"] == SIMULATION_ERROR
    assert response.get_json()["run"]["status"] == "failed"
    assert_no_internals(response.get_data(as_text=True), app)


def test_failed_run_readable_without_details(client, app):
    with patch("app.services.simulation.subprocess.run", side_effect=FAILURES["process_error"]), fake_metrics(
        lambda path: STATS
    ):
        run_id = post_run(client).get_json()["run"]["id"]
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.get_json()["status"] == "failed"
    assert_no_internals(response.get_data(as_text=True), app)
    listed = client.get("/api/runs?controller=fixed")
    assert [r["status"] for r in listed.get_json()] == ["failed"]
    assert_no_internals(listed.get_data(as_text=True), app)


def test_validation_errors_unchanged(client):
    assert post_run(client, "magic").get_json() == {
        "error": "unknown controller",
        "allowed": ["fixed", "queue_based", "wait_time"],
    }
    assert client.post("/api/runs", json={"scenario_id": 999, "controller": "fixed"}).get_json() == {
        "error": "scenario not found"
    }
    assert client.get("/api/scenarios").status_code == 200
    assert client.get("/api/scenarios/999").status_code == 404
