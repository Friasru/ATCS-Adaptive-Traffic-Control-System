import pytest


def valid_payload(**overrides):
    payload = {
        "name": "rush_hour",
        "description": "Heavy traffic in both directions",
        "ew_vehs_per_hour": 900,
        "ns_vehs_per_hour": 700,
    }
    payload.update(overrides)
    return payload


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_list_contains_seeded_scenario(client):
    response = client.get("/api/scenarios")
    data = response.get_json()
    assert response.status_code == 200
    assert len(data) == 1
    assert data[0]["name"] == "uneven"
    assert data[0]["ew_vehs_per_hour"] == 500
    assert data[0]["ns_vehs_per_hour"] == 200


def test_get_scenario(client):
    response = client.get("/api/scenarios/1")
    assert response.status_code == 200
    assert response.get_json()["name"] == "uneven"


def test_get_missing_scenario(client):
    response = client.get("/api/scenarios/999")
    assert response.status_code == 404
    assert response.get_json() == {"error": "scenario not found"}


def test_create_scenario(client):
    response = client.post("/api/scenarios", json=valid_payload())
    data = response.get_json()
    assert response.status_code == 201
    assert data["name"] == "rush_hour"
    assert data["ew_vehs_per_hour"] == 900
    assert data["ns_vehs_per_hour"] == 700
    assert len(client.get("/api/scenarios").get_json()) == 2


def test_create_scenario_strips_name_and_allows_missing_description(client):
    payload = valid_payload(name="  light  ")
    del payload["description"]
    response = client.post("/api/scenarios", json=payload)
    assert response.status_code == 201
    assert response.get_json()["name"] == "light"
    assert response.get_json()["description"] is None


def test_create_scenario_duplicate_name(client):
    response = client.post("/api/scenarios", json=valid_payload(name="uneven"))
    assert response.status_code == 409
    assert "already exists" in response.get_json()["error"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 65},
        {"name": 123},
        {"description": "x" * 256},
        {"ew_vehs_per_hour": -1},
        {"ew_vehs_per_hour": 1501},
        {"ns_vehs_per_hour": "lots"},
        {"ns_vehs_per_hour": 12.5},
        {"ns_vehs_per_hour": True},
        {"ew_vehs_per_hour": 0, "ns_vehs_per_hour": 0},
    ],
)
def test_create_scenario_rejects_invalid_input(client, overrides):
    response = client.post("/api/scenarios", json=valid_payload(**overrides))
    assert response.status_code == 400
    assert response.get_json()["errors"]


def test_create_scenario_without_body(client):
    response = client.post("/api/scenarios")
    assert response.status_code == 400
    assert len(response.get_json()["errors"]) >= 1


def test_create_scenario_accepts_boundary_values(client):
    response = client.post(
        "/api/scenarios",
        json=valid_payload(name="edge", ew_vehs_per_hour=1500, ns_vehs_per_hour=0),
    )
    assert response.status_code == 201
