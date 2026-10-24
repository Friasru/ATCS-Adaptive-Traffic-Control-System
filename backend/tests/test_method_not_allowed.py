"""GET on a POST-only /api route answers 405, without changing static files or other 404s."""

import pytest


@pytest.mark.parametrize("path,allowed", [
    ("/api/auth/login", "OPTIONS, POST"),
    ("/api/auth/register", "OPTIONS, POST"),
    ("/api/monitor/start", "OPTIONS, POST"),
    ("/api/monitor/recording/stop", "OPTIONS, POST"),
])
def test_get_on_post_only_api_route_is_405(client, path, allowed):
    r = client.get(path)
    assert r.status_code == 405
    assert r.get_json() == {"error": "method not allowed"}
    assert r.headers["Allow"] == allowed


def test_other_wrong_methods_still_405(client):
    assert client.post("/api/health").status_code == 405
    assert client.delete("/api/scenarios").status_code == 405


@pytest.mark.parametrize("path", ["/dashboard.html", "/scenarios.html", "/js/dashboard.js", "/css/dashboard.css",
                                  "/img/logo.svg"])
def test_static_files_unchanged(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", ["/api/nope", "/api/scenarios/999999", "/api/runs/999999", "/missing.html",
                                  "/js/missing.js", "/api/monitor/../../etc/passwd"])
def test_not_found_unchanged(client, path):
    assert client.get(path).status_code == 404


def test_post_to_static_path_unchanged(client):
    assert client.post("/dashboard.html").status_code == 405
    assert client.post("/missing.html").status_code == 405


def test_get_routes_still_work(client, auth_headers):
    assert client.get("/api/health").get_json() == {"status": "ok"}
    assert client.get("/api/scenarios").status_code == 200
    assert client.get("/api/compare?scenario_id=1", headers=auth_headers).status_code == 200
