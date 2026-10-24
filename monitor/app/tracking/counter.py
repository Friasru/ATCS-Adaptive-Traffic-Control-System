"""Counting engine: visible, unique-observed and line-crossing counts.

The three metrics are deliberately kept separate:

* visible   - objects present in the *current* frame (recomputed every frame)
* unique    - confirmed tracker IDs seen during the session (each ID once)
* crossings - confirmed tracks whose anchor point crossed a counting line

Line-crossing rules
-------------------
For each (track, line) pair the engine remembers the last *stable* side of the
line and the last anchor position observed on that side. A dead zone of
``hysteresis_frac`` x frame diagonal around the line is ignored, so jitter on the
line never flips the side. A crossing is registered only when the track reappears
on the opposite stable side **and**

* the segment between the last stable position and the new position intersects
  the finite line segment (passing around the end of the line is not a crossing),
* the anchor has travelled at least ``min_travel_frac`` (0.4) x the object's size from
  its farthest point on the old side. The size is the box extent across the line,
  the smaller of its value at that farthest point and now (capped at
  ``max_travel_frac`` (0.2) x diagonal, so boxes as large as the frame stay
  countable; a lower cap let a seated person leaning towards the webcam count).
  Until then the old side is kept, so a slow crossing is simply counted later.

The travel rule exists because the centre of a large box drifts by tens of pixels
while the object stays put - a person sitting in front of a webcam shifts posture,
a partly occluded box changes shape - and the fixed band turned that drift into
pairs of opposite crossings. Taking the smaller of the two sizes keeps objects
countable that grow quickly while approaching the camera (small at the far point)
or shrink while driving away (small now); it is strict only for objects that are
large throughout - the ones whose centre drifts. For
the ``bottom_center`` anchor the size is at most the box's thinner side (its
footprint): feet move far less than the body height. See docs/counting_accuracy.md.

By default each track is counted at most once per line per direction.

Side "A" is the right-hand side when walking from P1 to P2 on screen; "a_to_b"
therefore means moving from the right-hand side to the left-hand side.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.detection.classes import MONITORED_CLASSES, zero_counts
from app.detection.types import Detection
from app.tracking.geometry import segments_intersect, signed_distance, valid_normalized_point

DIRECTIONS = ("both", "a_to_b", "b_to_a")
ANCHORS = ("center", "bottom_center")


@dataclass
class CountingLine:
    line_id: str = "line-1"
    p1: tuple[float, float] = (0.1, 0.5)
    p2: tuple[float, float] = (0.9, 0.5)
    direction: str = "both"
    enabled: bool = True

    def validate(self) -> None:
        if not self.line_id or not isinstance(self.line_id, str):
            raise ValueError("Line id must be a non-empty string")
        if not (valid_normalized_point(self.p1) and valid_normalized_point(self.p2)):
            raise ValueError("Line endpoints must be finite and inside the frame (0..1)")
        if math.hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1]) < 0.02:
            raise ValueError("Line endpoints are too close together")
        if self.direction not in DIRECTIONS:
            raise ValueError(f"Unknown direction '{self.direction}'")

    def to_dict(self) -> dict:
        return {
            "line_id": self.line_id,
            "p1": [float(self.p1[0]), float(self.p1[1])],
            "p2": [float(self.p2[0]), float(self.p2[1])],
            "direction": self.direction,
            "enabled": bool(self.enabled),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CountingLine":
        line = cls(
            line_id=str(d.get("line_id", "line-1")),
            p1=(float(d["p1"][0]), float(d["p1"][1])),
            p2=(float(d["p2"][0]), float(d["p2"][1])),
            direction=str(d.get("direction", "both")),
            enabled=bool(d.get("enabled", True)),
        )
        line.validate()
        return line

    def pixels(self, width: int, height: int) -> tuple[tuple[float, float], tuple[float, float]]:
        return (self.p1[0] * width, self.p1[1] * height), (self.p2[0] * width, self.p2[1] * height)


@dataclass(frozen=True)
class CrossingEvent:
    timestamp: float
    line_id: str
    track_id: int
    cls: str
    direction: str  # "a_to_b" | "b_to_a"


@dataclass(frozen=True)
class TrackEvent:
    timestamp: float
    event_type: str  # "track_confirmed" | "track_lost"
    track_id: int
    cls: str
    confidence: float
    bbox: tuple[float, float, float, float]


@dataclass
class _LineState:
    side: int = 0  # +1 side A, -1 side B, 0 unknown
    pos: Optional[tuple[float, float]] = None
    far: float = 0.0  # signed distance of the farthest point seen on the current side
    need: float = 0.0  # travel from `far` required to count a crossing


@dataclass
class _Track:
    track_id: int
    first_frame: int
    last_frame: int
    last_seen: float
    hits: int = 0
    votes: Counter = field(default_factory=Counter)
    confirmed: bool = False
    confidence: float = 0.0
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    lines: dict = field(default_factory=dict)  # line_id -> _LineState
    counted: set = field(default_factory=set)  # {(line_id, direction)}
    pending: list = field(default_factory=list)  # crossings awaiting confirmation
    size: Optional[tuple[float, float]] = None  # smoothed box (width, height)

    @property
    def label(self) -> str:
        return self.votes.most_common(1)[0][0]


@dataclass
class FrameUpdate:
    visible: dict
    unique: dict
    crossings: dict
    crossing_events: list
    track_events: list


class CountingEngine:
    def __init__(
        self,
        lines: Iterable[CountingLine] = (),
        min_hits: int = 3,
        hysteresis_frac: float = 0.01,
        anchor: str = "center",
        max_age_frames: int = 300,
        enabled_classes: Optional[Iterable[str]] = None,
        min_travel_frac: float = 0.4,
        max_travel_frac: float = 0.2,
    ) -> None:
        if min_hits < 1:
            raise ValueError("min_hits must be >= 1")
        if not 0.0 <= hysteresis_frac < 0.25:
            raise ValueError("hysteresis_frac must be in [0, 0.25)")
        if not 0.0 <= min_travel_frac <= 2.0:
            raise ValueError("min_travel_frac must be in [0, 2]")
        if not 0.0 <= max_travel_frac <= 1.0:
            raise ValueError("max_travel_frac must be in [0, 1]")
        if anchor not in ANCHORS:
            raise ValueError(f"Unknown anchor '{anchor}'")
        self.min_hits = min_hits
        self.hysteresis_frac = hysteresis_frac
        self.min_travel_frac = min_travel_frac
        self.max_travel_frac = max_travel_frac
        self.anchor = anchor
        self.max_age_frames = max_age_frames
        self.enabled_classes = set(enabled_classes) if enabled_classes is not None else set(MONITORED_CLASSES)
        self.lines: dict[str, CountingLine] = {}
        self.set_lines(lines)
        self.reset()

    # ------------------------------------------------------------------ state
    def reset(self) -> None:
        """Forget all tracks and zero every counter (new session / stream)."""
        self._tracks: dict[int, _Track] = {}
        self.frame_index = 0
        self.visible = zero_counts()
        self.unique = zero_counts()
        self.crossings = zero_counts()
        self.line_totals: dict[str, dict[str, dict[str, int]]] = {}
        for lid in self.lines:
            self._init_line_totals(lid)
        self.peak_visible_total = 0
        self._visible_sum = 0
        self._frames_since_reset = 0

    def _init_line_totals(self, lid: str) -> None:
        self.line_totals[lid] = {"a_to_b": zero_counts(), "b_to_a": zero_counts()}

    def set_lines(self, lines: Iterable[CountingLine]) -> None:
        """Replace counting lines. Moved/changed lines lose their side history
        so that relocating a line never produces phantom crossings."""
        new = {}
        for ln in lines:
            ln.validate()
            if ln.line_id in new:
                raise ValueError(f"Duplicate line id '{ln.line_id}'")
            new[ln.line_id] = CountingLine(
                ln.line_id,
                (float(ln.p1[0]), float(ln.p1[1])),
                (float(ln.p2[0]), float(ln.p2[1])),
                ln.direction,
                ln.enabled,
            )
        old = getattr(self, "lines", {})
        changed = {lid for lid, ln in new.items() if lid not in old or old[lid] != ln}
        self.lines = new
        if hasattr(self, "_tracks"):
            for t in self._tracks.values():
                for lid in list(t.lines):
                    if lid in changed or lid not in new:
                        del t.lines[lid]
                t.pending = [p for p in t.pending if p.line_id in new and p.line_id not in changed]
            for lid in new:
                if lid not in self.line_totals:
                    self._init_line_totals(lid)

    def set_enabled_classes(self, classes: Iterable[str]) -> None:
        self.enabled_classes = set(classes)

    @property
    def active_track_count(self) -> int:
        return len(self._tracks)

    @property
    def average_visible_total(self) -> float:
        n = self._frames_since_reset
        return self._visible_sum / n if n else 0.0

    # ----------------------------------------------------------------- update
    def update(self, detections: Iterable[Detection], frame_size: tuple[int, int], timestamp: float) -> FrameUpdate:
        width, height = frame_size
        self.frame_index += 1
        self._frames_since_reset += 1
        diag = math.hypot(width, height)
        tol = self.hysteresis_frac * diag
        max_need = self.max_travel_frac * diag
        crossing_events: list[CrossingEvent] = []
        track_events: list[TrackEvent] = []
        visible = zero_counts()
        seen_ids = set()

        for det in detections:
            if det.cls not in self.enabled_classes or det.cls not in visible:
                continue
            visible[det.cls] += 1
            if det.track_id is None:
                continue
            tid = int(det.track_id)
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            t = self._tracks.get(tid)
            if t is None:
                t = _Track(tid, self.frame_index, self.frame_index, timestamp)
                self._tracks[tid] = t
            t.last_frame = self.frame_index
            t.last_seen = timestamp
            t.hits += 1
            t.votes[det.cls] += 1
            t.confidence = det.confidence
            t.bbox = (det.x1, det.y1, det.x2, det.y2)
            w, h = det.x2 - det.x1, det.y2 - det.y1
            t.size = (w, h) if t.size is None else (0.8 * t.size[0] + 0.2 * w, 0.8 * t.size[1] + 0.2 * h)

            if not t.confirmed and t.hits >= self.min_hits:
                t.confirmed = True
                self.unique[t.label] += 1
                track_events.append(TrackEvent(timestamp, "track_confirmed", tid, t.label, t.confidence, t.bbox))
                for ev in t.pending:
                    crossing_events.append(self._commit(t, ev.line_id, ev.direction, ev.timestamp))
                t.pending.clear()

            pos = det.anchor(self.anchor)
            for line in self.lines.values():
                if not line.enabled:
                    continue
                need = min(self.min_travel_frac * self._extent(t, line, width, height), max_need)
                direction = self._check_line(t, line, pos, width, height, tol, need)
                if direction is None:
                    continue
                if line.direction != "both" and line.direction != direction:
                    continue
                key = (line.line_id, direction)
                if key in t.counted or any(p.line_id == line.line_id and p.direction == direction for p in t.pending):
                    continue
                if t.confirmed:
                    crossing_events.append(self._commit(t, line.line_id, direction, timestamp))
                else:
                    t.pending.append(CrossingEvent(timestamp, line.line_id, tid, t.label, direction))

        # Expire tracks that have not been seen for a long time.
        for tid in [tid for tid, t in self._tracks.items() if self.frame_index - t.last_frame > self.max_age_frames]:
            t = self._tracks.pop(tid)
            if t.confirmed:
                track_events.append(TrackEvent(timestamp, "track_lost", tid, t.label, t.confidence, t.bbox))

        self.visible = visible
        total = sum(visible.values())
        self.peak_visible_total = max(self.peak_visible_total, total)
        self._visible_sum += total
        return FrameUpdate(dict(visible), dict(self.unique), dict(self.crossings), crossing_events, track_events)

    def _commit(self, t: _Track, line_id: str, direction: str, timestamp: float) -> CrossingEvent:
        cls = t.label
        t.counted.add((line_id, direction))
        self.crossings[cls] += 1
        self.line_totals[line_id][direction][cls] += 1
        return CrossingEvent(timestamp, line_id, t.track_id, cls, direction)

    def _extent(self, t: _Track, line: CountingLine, width: int, height: int) -> float:
        """The object's (smoothed) box size across the line, in pixels."""
        if t.size is None:
            return 0.0
        (ax, ay), (bx, by) = line.pixels(width, height)
        length = math.hypot(bx - ax, by - ay) or 1.0
        extent = abs(t.size[0] * (by - ay) / length) + abs(t.size[1] * (bx - ax) / length)
        if self.anchor == "bottom_center":
            extent = min(extent, *t.size)  # footprint: feet move far less than the body height
        return extent

    @staticmethod
    def _check_line(t: _Track, line: CountingLine, pos, width, height, tol, need) -> Optional[str]:
        a, b = line.pixels(width, height)
        d = signed_distance(pos, a, b)
        st = t.lines.get(line.line_id)
        if st is None:
            st = t.lines[line.line_id] = _LineState()
        if abs(d) <= tol:
            return None  # inside the hysteresis band: no decision
        side = 1 if d > 0 else -1
        if st.side == 0 or side == st.side:
            if st.side == 0 or abs(d) > abs(st.far):
                st.far, st.need = d, need
            st.side, st.pos = side, pos
            return None
        assert st.pos is not None
        if not segments_intersect(st.pos, pos, a, b):
            st.side, st.pos, st.far, st.need = side, pos, d, need
            return None  # went around the end of the line
        if abs(d - st.far) < min(st.need, need):
            return None  # not enough travel yet (drift): keep the old side
        prev = st.side
        st.side, st.pos, st.far, st.need = side, pos, d, need
        return "a_to_b" if prev == 1 else "b_to_a"

    def finalize(self, timestamp: float) -> list[TrackEvent]:
        """Emit track_lost for every remaining confirmed track (end of session)."""
        events = [
            TrackEvent(timestamp, "track_lost", t.track_id, t.label, t.confidence, t.bbox)
            for t in self._tracks.values()
            if t.confirmed
        ]
        self._tracks.clear()
        return events
