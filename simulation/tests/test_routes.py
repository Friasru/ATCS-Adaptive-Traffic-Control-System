import xml.etree.ElementTree as ET

import runner


def read_routes(path):
    root = ET.parse(path).getroot()
    flows = {f.get("id"): int(f.get("vehsPerHour")) for f in root.findall("flow")}
    routes = {r.get("id"): r.get("edges") for r in root.findall("route")}
    return flows, routes


def test_write_routes_uses_rates_for_each_direction(tmp_path):
    path = tmp_path / "routes.rou.xml"
    runner.write_routes(str(path), 300, 700)
    flows, _ = read_routes(path)
    assert flows == {"f_we": 300, "f_ew": 300, "f_ns": 700, "f_sn": 700}


def test_write_routes_skips_zero_flows(tmp_path):
    path = tmp_path / "routes.rou.xml"
    runner.write_routes(str(path), 500, 0)
    flows, _ = read_routes(path)
    assert flows == {"f_we": 500, "f_ew": 500}


def test_write_routes_defines_all_routes(tmp_path):
    path = tmp_path / "routes.rou.xml"
    runner.write_routes(str(path), 100, 100)
    _, routes = read_routes(path)
    assert routes == {
        "west_east": "left0A0 A0right0",
        "east_west": "right0A0 A0left0",
        "north_south": "top0A0 A0bottom0",
        "south_north": "bottom0A0 A0top0",
    }
