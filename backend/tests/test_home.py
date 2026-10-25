def test_home_page_is_served_at_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    html = response.get_data(as_text=True)
    assert "Adaptive Traffic Control System" in html
    for page in ("scenarios.html", "dashboard.html", "monitor.html"):
        assert f'href="{page}"' in html
