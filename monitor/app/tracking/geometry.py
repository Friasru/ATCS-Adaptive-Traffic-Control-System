"""2-D geometry helpers for counting lines."""

from __future__ import annotations

import math

Point = tuple[float, float]


def cross(o: Point, a: Point, b: Point) -> float:
    """Z component of (a - o) x (b - o)."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def signed_distance(p: Point, a: Point, b: Point) -> float:
    """Signed perpendicular distance from p to the infinite line a->b.

    In image coordinates (y grows downwards) a positive value means p lies on
    the right-hand side of someone walking from a to b on screen. That side is
    called side "A" by the counting engine.
    """
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length == 0:
        return 0.0
    return cross(a, b, p) / length


def _on_segment(p: Point, q: Point, r: Point) -> bool:
    return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])


def segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """True if closed segments p1-p2 and q1-q2 share at least one point."""
    d1 = cross(q1, q2, p1)
    d2 = cross(q1, q2, p2)
    d3 = cross(p1, p2, q1)
    d4 = cross(p1, p2, q2)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True
    if d1 == 0 and _on_segment(q1, p1, q2):
        return True
    if d2 == 0 and _on_segment(q1, p2, q2):
        return True
    if d3 == 0 and _on_segment(p1, q1, p2):
        return True
    if d4 == 0 and _on_segment(p1, q2, p2):
        return True
    return False


def valid_normalized_point(p) -> bool:
    try:
        x, y = float(p[0]), float(p[1])
    except (TypeError, ValueError, IndexError):
        return False
    return math.isfinite(x) and math.isfinite(y) and 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
