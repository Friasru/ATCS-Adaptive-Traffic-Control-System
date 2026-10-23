import csv
import io

import pytest

from app import db
from app.api.export import COLUMNS
from app.models import Run


def add_run(app, **fields):
    values = {"scenario_id": 1, "controller": "fixed", "status": "completed", "avg_wait_s": 6.5}
    values.update(fields)
    with app.app_context():
        run = Run(**values)
        db.session.add(run)
        db.session.commit()
        return run.id


def export(client, headers, query=""):
    return client.get("/api/runs/export.csv" + query, headers=headers)


def rows(response):
    return list(csv.DictReader(io.StringIO(response.get_data(as_text=True))))


def other_scenario(client):
    return client.post(
        "/api/scenarios", json={"name": "other", "ew_vehs_per_hour": 100, "ns_vehs_per_hour": 100}
    ).get_json()["id"]


def test_headers(client, app, auth_headers):
    add_run(app)
    response = export(client, auth_headers)
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    disposition = response.headers["Content-Disposition"]
    assert disposition.startswith("attachment")
    assert "atcs_runs.csv" in disposition


def test_columns_match_run_to_dict(client, app, auth_headers):
    run_id = add_run(app)
    response = export(client, auth_headers)
    header = response.get_data(as_text=True).splitlines()[0].split(",")
    assert header == COLUMNS
    with app.app_context():
        assert list(db.session.get(Run, run_id).to_dict()) == COLUMNS


def test_values_and_missing_values(client, app, auth_headers):
    run_id = add_run(app, vehicles_completed=1400, max_wait_s=None)
    (row,) = rows(export(client, auth_headers))
    assert row["id"] == str(run_id)
    assert row["controller"] == "fixed"
    assert row["avg_wait_s"] == "6.5"
    assert row["vehicles_completed"] == "1400"
    assert row["max_wait_s"] == ""
    assert row["created_at"]


def test_escaping(client, app, auth_headers):
    # The API only creates allow-listed controllers; this checks the CSV writer
    # itself if odd text ever reaches the table.
    add_run(app, controller='a,"b"\nc')
    add_run(app, controller="=HYPERLINK(1)")
    parsed = rows(export(client, auth_headers))
    assert parsed[0]["controller"] == 'a,"b"\nc'
    assert parsed[1]["controller"] == "'=HYPERLINK(1)"
    assert len(parsed) == 2


def test_negative_numbers_not_altered(client, app, auth_headers):
    add_run(app, avg_wait_s=-1.5)
    assert rows(export(client, auth_headers))[0]["avg_wait_s"] == "-1.5"


def test_ordered_by_id(client, app, auth_headers):
    ids = [add_run(app, controller=c) for c in ("wait_time", "fixed", "queue_based")]
    assert [int(r["id"]) for r in rows(export(client, auth_headers))] == ids


def test_scenario_filter(client, app, auth_headers):
    other = other_scenario(client)
    add_run(app)
    add_run(app, scenario_id=other)
    parsed = rows(export(client, auth_headers, f"?scenario_id={other}"))
    assert [r["scenario_id"] for r in parsed] == [str(other)]


def test_controller_filter(client, app, auth_headers):
    add_run(app)
    add_run(app, controller="wait_time")
    parsed = rows(export(client, auth_headers, "?controller=wait_time"))
    assert [r["controller"] for r in parsed] == ["wait_time"]


def test_combined_filters(client, app, auth_headers):
    other = other_scenario(client)
    add_run(app, controller="wait_time")
    target = add_run(app, scenario_id=other, controller="wait_time")
    add_run(app, scenario_id=other, controller="fixed")
    parsed = rows(export(client, auth_headers, f"?scenario_id={other}&controller=wait_time"))
    assert [int(r["id"]) for r in parsed] == [target]


def test_includes_all_statuses(client, app, auth_headers):
    add_run(app, status="failed", avg_wait_s=None)
    add_run(app)
    assert [r["status"] for r in rows(export(client, auth_headers))] == ["failed", "completed"]


def test_empty_result_has_header_only(client, auth_headers):
    response = export(client, auth_headers, "?scenario_id=999")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == ",".join(COLUMNS) + "\r\n"


@pytest.mark.parametrize(
    "query",
    ["?scenario_id=abc", "?scenario_id=", "?scenario_id=-1", "?controller=magic", "?controller="],
)
def test_invalid_filters(client, auth_headers, query):
    assert export(client, auth_headers, query).status_code == 400


def test_requires_auth(client, app):
    add_run(app)
    response = client.get("/api/runs/export.csv")
    assert response.status_code == 401
    assert "fixed" not in response.get_data(as_text=True)


def test_does_not_shadow_run_routes(client, app):
    run_id = add_run(app)
    assert client.get(f"/api/runs/{run_id}").status_code == 200
    assert client.get("/api/runs").status_code == 200
