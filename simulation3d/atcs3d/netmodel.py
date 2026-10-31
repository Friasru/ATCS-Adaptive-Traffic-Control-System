"""Turn the SUMO network file into the JSON the 3D renderer builds the road from.

The renderer never invents geometry: lane shapes, the junction polygon and the signal-link layout all come from
the same `intersection_final.net.xml` that SUMO simulates, so roads line up with the simulated trajectories.
"""
import xml.etree.ElementTree as ET

from . import paths


def _points(shape):
    return [[round(float(v), 3) for v in p.split(",")[:2]] for p in shape.split()]


def load_network(net_file=paths.NET_FILE, tls_id=paths.TLS_ID):
    root = ET.parse(net_file).getroot()
    loc = root.find("location")
    x0, y0, x1, y1 = (float(v) for v in loc.get("convBoundary").split(","))

    edges = []
    lane_of = {}
    for e in root.findall("edge"):
        if e.get("function") == "internal":
            continue
        lanes = []
        for ln in e.findall("lane"):
            lane = {
                "id": ln.get("id"),
                "index": int(ln.get("index")),
                "length": float(ln.get("length")),
                "speed": float(ln.get("speed")),
                "width": float(ln.get("width", 3.2)),  # SUMO's default lane width
                "shape": _points(ln.get("shape")),
            }
            lanes.append(lane)
            lane_of[lane["id"]] = lane
        edges.append({"id": e.get("id"), "from": e.get("from"), "to": e.get("to"), "lanes": lanes})

    junctions = []
    for j in root.findall("junction"):
        if j.get("type") == "internal":
            continue
        junctions.append(
            {
                "id": j.get("id"),
                "type": j.get("type"),
                "x": float(j.get("x")),
                "y": float(j.get("y")),
                "shape": _points(j.get("shape")) if j.get("shape") else [],
            }
        )

    tl = root.find(f"tlLogic[@id='{tls_id}']")
    phases = [{"duration": float(p.get("duration")), "state": p.get("state")} for p in tl.findall("phase")]

    links = []
    for c in root.findall("connection"):
        if c.get("tl") != tls_id:
            continue
        links.append(
            {
                "index": int(c.get("linkIndex")),
                "from_edge": c.get("from"),
                "from_lane": f"{c.get('from')}_{c.get('fromLane')}",
                "to_edge": c.get("to"),
                "dir": c.get("dir"),  # r, s, l, t (right, straight, left, u-turn)
            }
        )
    links.sort(key=lambda l: l["index"])

    # One approach per incoming lane. Its signal head follows the straight-ahead link of that lane (all demand
    # in the ATCS scenarios goes straight). Heading/stop line come from the last segment of the lane shape.
    approaches = []
    seen = []
    for l in links:
        if l["from_lane"] not in seen:
            seen.append(l["from_lane"])
    for lane_id in seen:
        lane = lane_of[lane_id]
        (px, py), (qx, qy) = lane["shape"][-2], lane["shape"][-1]
        mine = [l for l in links if l["from_lane"] == lane_id]
        straight = next(l for l in mine if l["dir"] == "s")
        approaches.append(
            {
                "lane": lane_id,
                "edge": lane_id.rsplit("_", 1)[0],
                "stop_point": [qx, qy],
                "dir_vec": [qx - px, qy - py],
                "straight_link": straight["index"],
                "links": [l["index"] for l in mine],
                "link_dirs": {str(l["index"]): l["dir"] for l in mine},
            }
        )

    return {
        "bounds": [x0, y0, x1, y1],
        "center": [(x0 + x1) / 2, (y0 + y1) / 2],
        "edges": edges,
        "junctions": junctions,
        "tls": {"id": tls_id, "phases": phases, "links": links},
        "approaches": approaches,
    }
