"""The 3D Simulator tab: the page is served and every page links to it right after Compare.

The simulation itself runs in the separate service in simulation3d/ (its tests live there, they need SUMO). Starting
and stopping that service is tested in test_simulator3d_service.py; nothing here starts it.
"""
import os
from html.parser import HTMLParser

import pytest

PAGES = ["index.html", "scenarios.html", "dashboard.html", "monitor.html", "simulator.html", "compare.html", "simulator3d.html"]
NAV = [
    ["Home", "index.html"],
    ["Scenarios", "scenarios.html"],
    ["Dashboard", "dashboard.html"],
    ["Monitor", "monitor.html"],
    ["Simulator", "simulator.html"],
    ["Compare", "compare.html"],
    ["3D Simulator", "simulator3d.html"],
]
REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class Page(HTMLParser):
    """Collects what the checks need: nav links, headings and the iframe."""

    def __init__(self):
        super().__init__()
        self.nav, self.h1, self.iframes, self.assets = [], "", [], []
        self._in_nav = self._in_h1 = False
        self._link = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "nav":
            self._in_nav = True
        elif tag == "a" and self._in_nav:
            self._link = {"href": a.get("href"), "current": a.get("aria-current"), "text": ""}
        elif tag == "h1":
            self._in_h1 = True
        elif tag == "iframe":
            self.iframes.append(a)
        elif tag == "script" and a.get("src"):
            self.assets.append(a["src"])
        elif tag == "link" and a.get("rel") == "stylesheet":
            self.assets.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "nav":
            self._in_nav = False
        elif tag == "a" and self._link is not None:
            self.nav.append(self._link)
            self._link = None
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._link is not None:
            self._link["text"] += data
        if self._in_h1:
            self.h1 += data


def parse(client, page):
    response = client.get("/" + page)
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    p = Page()
    p.feed(response.get_data(as_text=True))
    return p


@pytest.mark.parametrize("page", PAGES)
def test_every_page_lists_the_tab_right_after_compare(client, page):
    p = parse(client, page)
    assert [[l["text"].strip(), l["href"]] for l in p.nav] == NAV
    assert [l["href"] for l in p.nav if l["current"]] == [page]  # each page marks itself, and only itself


def test_3d_page_has_the_title_the_explanation_and_the_frame(client):
    html = client.get("/simulator3d.html").get_data(as_text=True)
    p = parse(client, "simulator3d.html")
    assert p.h1.strip() == "3D Simulator"
    assert "Explore real SUMO traffic simulations in 3D using the ATCS Fixed, Queue-Based, and Wait-Time controllers." in html
    assert "simplified model written for the browser" in html  # tells it apart from Ian's 2D pages
    assert [f.get("id") for f in p.iframes] == ["stage"]
    assert "fullscreen" in p.iframes[0].get("allow", "")  # the 3D page's F key needs it


def test_3d_page_assets_are_served(client):
    for src in parse(client, "simulator3d.html").assets:
        assert client.get("/" + src).status_code == 200, src


def test_page_and_launcher_agree_on_the_service_port(client):
    js = client.get("/js/simulator3d.js").get_data(as_text=True)
    with open(os.path.join(REPO, "simulation3d", "run.sh")) as f:
        launcher = f.read()
    assert "DEFAULT_PORT = 8766" in js
    assert "ATCS3D_PORT:-8766" in launcher


def test_the_page_has_somewhere_to_explain_a_failed_start(client):
    html = client.get("/simulator3d.html").get_data(as_text=True)
    for element in ('id="unavailable-reason"', 'id="unavailable-detail"', 'id="retry"', "simulation3d/run.sh"):
        assert element in html


def test_the_2d_pages_are_not_replaced_or_renamed(client):
    compare = parse(client, "compare.html")
    assert compare.h1.strip() == "Compare controllers"
    assert parse(client, "simulator.html").h1.strip() == "Traffic simulator"
