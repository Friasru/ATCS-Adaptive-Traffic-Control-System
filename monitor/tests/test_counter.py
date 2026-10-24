"""Deterministic counting scenarios (synthetic tracks, no model involved)."""

import random

import pytest

from app.detection.types import Detection
from app.tracking.counter import CountingEngine, CountingLine

W, H = 1000, 1000


def box(cls, tid, cx, cy, size=40, conf=0.9):
    return Detection(cls, conf, cx - size / 2, cy - size / 2, cx + size / 2, cy + size / 2, tid)


def horizontal(direction="both", y=0.5):
    # P1 left, P2 right: side A (right-hand side walking P1->P2) is BELOW the line.
    return CountingLine("line-1", (0.1, y), (0.9, y), direction, True)


def run(engine, frames):
    events = []
    for i, dets in enumerate(frames):
        upd = engine.update(dets, (W, H), float(i))
        events.extend(upd.crossing_events)
    return events


def path(cls, tid, x, y0, y1, steps):
    return [[box(cls, tid, x, y0 + (y1 - y0) * k / (steps - 1))] for k in range(steps)]


def test_one_car_crossing_once_downwards_is_b_to_a():
    eng = CountingEngine([horizontal()])
    ev = run(eng, path("car", 1, 500, 300, 700, 12))
    assert [(e.cls, e.direction, e.track_id) for e in ev] == [("car", "b_to_a", 1)]
    assert eng.crossings["car"] == 1
    assert eng.unique["car"] == 1


def test_person_crossing_opposite_direction():
    eng = CountingEngine([horizontal()])
    ev = run(eng, path("person", 7, 400, 700, 300, 12))
    assert [(e.cls, e.direction) for e in ev] == [("person", "a_to_b")]


def test_stationary_car_never_counted():
    eng = CountingEngine([horizontal()])
    ev = run(eng, [[box("car", 1, 500, 520)] for _ in range(100)])
    assert ev == []
    assert eng.crossings["car"] == 0
    assert eng.unique["car"] == 1
    assert eng.visible["car"] == 1


def test_visible_counts_do_not_accumulate():
    eng = CountingEngine([horizontal()])
    for i in range(10):
        eng.update([box("car", 1, 100, 100), box("car", 2, 300, 100), box("person", 3, 500, 100)], (W, H), i)
    assert eng.visible == {"person": 1, "car": 2, "motorcycle": 0, "bicycle": 0, "bus": 0, "truck": 0}
    eng.update([box("car", 1, 100, 100)], (W, H), 11)
    assert eng.visible["car"] == 1 and eng.visible["person"] == 0


def test_untracked_detections_count_as_visible_only():
    eng = CountingEngine([horizontal()])
    for i in range(5):
        eng.update([Detection("car", 0.8, 0, 0, 10, 10, None)], (W, H), i)
    assert eng.visible["car"] == 1
    assert eng.unique["car"] == 0


def test_two_objects_crossing_simultaneously():
    eng = CountingEngine([horizontal()])
    frames = [
        [box("car", 1, 300, 300 + 40 * k), box("motorcycle", 2, 700, 300 + 40 * k)] for k in range(11)
    ]
    ev = run(eng, frames)
    assert sorted((e.cls, e.track_id) for e in ev) == [("car", 1), ("motorcycle", 2)]
    assert eng.crossings["car"] == 1 and eng.crossings["motorcycle"] == 1


@pytest.mark.parametrize("cls", ["bicycle", "motorcycle", "bus", "truck"])
def test_each_vehicle_class_counted_in_its_own_bucket(cls):
    eng = CountingEngine([horizontal()])
    run(eng, path(cls, 1, 500, 300, 700, 12))
    assert eng.crossings[cls] == 1
    assert sum(eng.crossings.values()) == 1  # e.g. a motorcycle is never a car


def test_track_disappearing_and_returning_with_same_id_not_double_counted():
    eng = CountingEngine([horizontal()])
    frames = path("car", 5, 500, 300, 700, 12)
    frames[5:8] = [[], [], []]  # occlusion around the line
    frames += [[] for _ in range(10)] + path("car", 5, 500, 700, 720, 5)
    ev = run(eng, frames)
    assert len(ev) == 1
    assert eng.unique["car"] == 1


def test_oscillation_near_line_is_absorbed_by_hysteresis():
    eng = CountingEngine([horizontal()], hysteresis_frac=0.01)  # ~14 px band
    frames = path("person", 1, 500, 400, 490, 6)
    for k in range(40):  # jitter +-8 px around the line
        frames.append([box("person", 1, 500, 500 + (8 if k % 2 else -8))])
    ev = run(eng, frames)
    assert ev == []


def test_oscillation_without_hysteresis_counts_at_most_once_per_direction():
    eng = CountingEngine([horizontal()], hysteresis_frac=0.0)
    frames = path("person", 1, 500, 400, 490, 6)
    for k in range(40):
        frames.append([box("person", 1, 500, 500 + (8 if k % 2 else -8))])
    ev = run(eng, frames)
    # Default policy: once per direction per track -> never more than 2.
    assert len(ev) <= 2
    assert len({e.direction for e in ev}) == len(ev)


def test_back_and_forth_counts_each_direction_once_only():
    eng = CountingEngine([horizontal()])
    frames = []
    for _ in range(3):
        frames += path("car", 1, 500, 300, 700, 8) + path("car", 1, 500, 700, 300, 8)
    ev = run(eng, frames)
    assert sorted(e.direction for e in ev) == ["a_to_b", "b_to_a"]


def test_direction_filter_a_to_b_only():
    eng = CountingEngine([horizontal("a_to_b")])
    run(eng, path("car", 1, 300, 300, 700, 12))  # downwards = b_to_a
    run(eng, path("car", 2, 600, 700, 300, 12))  # upwards = a_to_b
    assert eng.crossings["car"] == 1
    assert eng.line_totals["line-1"]["a_to_b"]["car"] == 1
    assert eng.line_totals["line-1"]["b_to_a"]["car"] == 0


def test_passing_beyond_line_end_is_not_a_crossing():
    eng = CountingEngine([horizontal()])
    ev = run(eng, path("car", 1, 960, 300, 700, 12))  # x=960 is right of P2 (x=900)
    assert ev == []


def test_track_id_switch_overcounts_and_this_is_visible():
    """Known limitation: if the tracker swaps a car's ID while it is on the
    line, the second ID can be counted again. This test documents it."""
    eng = CountingEngine([horizontal()], min_hits=1)
    frames = path("car", 1, 500, 300, 520, 8)  # crosses with id 1
    frames += [[box("car", 2, 500, 470)]]  # same physical car, new id, back above
    frames += path("car", 2, 500, 480, 700, 6)[1:]  # ...and crosses again
    ev = run(eng, frames)
    assert len(ev) == 2  # one real crossing counted twice
    assert eng.unique["car"] == 2


def test_unconfirmed_short_track_is_not_counted():
    eng = CountingEngine([horizontal()], min_hits=3)
    frames = [[box("car", 9, 500, 480)], [box("car", 9, 500, 530)]]  # only 2 hits
    ev = run(eng, frames + [[] for _ in range(5)])
    assert ev == [] and eng.unique["car"] == 0


def test_crossing_before_confirmation_is_committed_once_confirmed():
    eng = CountingEngine([horizontal()], min_hits=3)
    frames = [[box("car", 9, 500, 450)], [box("car", 9, 500, 560)], [box("car", 9, 500, 600)]]
    ev = run(eng, frames)
    assert [(e.track_id, e.direction) for e in ev] == [(9, "b_to_a")]


def test_line_moved_mid_session_produces_no_phantom_crossing():
    eng = CountingEngine([horizontal(y=0.5)])
    run(eng, [[box("car", 1, 500, 400)] for _ in range(5)])  # parked above y=500
    eng.set_lines([horizontal(y=0.3)])  # line moved above the parked car
    ev = run(eng, [[box("car", 1, 500, 400)] for _ in range(5)])
    assert ev == []
    ev = run(eng, path("car", 1, 500, 400, 100, 8))  # now it really crosses the moved line
    assert [e.direction for e in ev] == ["a_to_b"]


def test_counts_kept_when_line_moved():
    eng = CountingEngine([horizontal()])
    run(eng, path("car", 1, 500, 300, 700, 12))
    eng.set_lines([horizontal(y=0.2)])
    assert eng.crossings["car"] == 1


def test_stream_restart_resets_everything():
    eng = CountingEngine([horizontal()])
    run(eng, path("car", 1, 500, 300, 700, 12))
    eng.reset()
    assert sum(eng.crossings.values()) == 0 and sum(eng.unique.values()) == 0
    run(eng, path("car", 1, 500, 300, 700, 12))  # tracker restarted, IDs reused
    assert eng.crossings["car"] == 1


def test_disabled_class_is_ignored():
    eng = CountingEngine([horizontal()], enabled_classes=["car"])
    run(eng, path("person", 1, 500, 300, 700, 12))
    assert sum(eng.crossings.values()) == 0 and eng.visible["person"] == 0


def test_majority_vote_class_for_flickering_labels():
    eng = CountingEngine([horizontal()])
    frames = path("car", 1, 500, 300, 700, 12)
    frames[2] = [box("truck", 1, 500, 380)]  # one-frame misclassification
    run(eng, frames)
    assert eng.crossings == {"person": 0, "car": 1, "motorcycle": 0, "bicycle": 0, "bus": 0, "truck": 0}


def test_bottom_center_anchor():
    eng = CountingEngine([horizontal()], anchor="bottom_center")
    # Box centre stays above the line but its bottom edge crosses it.
    frames = [[Detection("person", 0.9, 480, 380 + 10 * k, 520, 470 + 10 * k, 1)] for k in range(8)]
    ev = run(eng, frames)
    assert len(ev) == 1


def test_stale_tracks_expire_and_emit_lost_event():
    eng = CountingEngine([horizontal()], max_age_frames=5)
    run(eng, [[box("car", 1, 100, 100)] for _ in range(4)])
    lost = []
    for i in range(7):
        lost += eng.update([], (W, H), 100 + i).track_events
    assert [e.event_type for e in lost] == ["track_lost"]
    assert eng.active_track_count == 0


def test_peak_and_average_visible():
    eng = CountingEngine([])
    eng.update([box("car", 1, 100, 100), box("car", 2, 300, 100)], (W, H), 0)
    eng.update([], (W, H), 1)
    assert eng.peak_visible_total == 2
    assert eng.average_visible_total == pytest.approx(1.0)


@pytest.mark.parametrize(
    "line",
    [
        CountingLine("x", (0.5, 0.5), (0.5, 0.5)),
        CountingLine("x", (-0.1, 0.5), (0.5, 0.5)),
        CountingLine("x", (0.1, 0.5), (1.5, 0.5)),
        CountingLine("x", (float("nan"), 0.5), (0.9, 0.5)),
        CountingLine("x", (0.1, 0.5), (0.9, 0.5), direction="sideways"),
        CountingLine("", (0.1, 0.5), (0.9, 0.5)),
    ],
)
def test_invalid_lines_rejected(line):
    with pytest.raises(ValueError):
        CountingEngine([line])


def test_duplicate_line_ids_rejected():
    with pytest.raises(ValueError):
        CountingEngine([horizontal(), horizontal(y=0.3)])


def test_two_lines_counted_independently():
    l1 = CountingLine("north", (0.1, 0.3), (0.9, 0.3))
    l2 = CountingLine("south", (0.1, 0.7), (0.9, 0.7))
    eng = CountingEngine([l1, l2])
    run(eng, path("car", 1, 500, 100, 900, 20))
    assert eng.line_totals["north"]["b_to_a"]["car"] == 1
    assert eng.line_totals["south"]["b_to_a"]["car"] == 1
    assert eng.crossings["car"] == 2  # crossings aggregate all lines


def test_line_roundtrip_dict():
    ln = CountingLine("l", (0.2, 0.3), (0.8, 0.6), "b_to_a", False)
    assert CountingLine.from_dict(ln.to_dict()) == ln


# ---------------------------------------------------------------------------
# Travel rule: jitter/drift, standing near the line, slow / reverse crossings,
# objects growing or shrinking with distance, and ID changes. Box sizes and
# trajectories are taken from real detections (docs/counting_accuracy.md).

def wander(cls, tid, cx, cy, w, h, n, amp, seed=1, axis=0, step=4.0):
    """A stationary object whose box centre drifts within +-amp px (posture changes)."""
    rng = random.Random(seed)
    out, off = [], 0.0
    for _ in range(n):
        off = max(-amp, min(amp, off + rng.uniform(-step, step)))
        x, y = (cx + off, cy) if axis == 0 else (cx, cy + off)
        out.append([Detection(cls, 0.8, x - w / 2, y - h / 2, x + w / 2, y + h / 2, tid)])
    return out


def vertical(x=0.5):
    return CountingLine("v", (x, 0.0), (x, 1.0), "both", True)


WEBCAM = (1280, 720)


def run_size(engine, frames, size):
    events = []
    for i, dets in enumerate(frames):
        events.extend(engine.update(dets, size, float(i)).crossing_events)
    return events


def test_person_sitting_on_the_line_is_never_counted():
    """Webcam case: a 757x427 px box straddling a vertical line whose centre drifts
    within +-55 px for a minute (measured); used to give pairs of opposite crossings."""
    frames = wander("person", 1, 640, 330, 757, 427, 900, 55)
    assert run_size(CountingEngine([vertical()]), frames, WEBCAM) == []
    # Without the travel rule the drift is counted (the bug this guards against).
    assert len(run_size(CountingEngine([vertical()], min_travel_frac=0.0), frames, WEBCAM)) >= 1


def test_partially_occluded_box_jumping_shape_is_not_counted():
    """Head-only vs full-body detections move the box centre back and forth across the line."""
    frames = []
    for k in range(200):
        if k % 7 < 3:
            frames.append([Detection("person", 0.6, 560, 120, 760, 330, 1)])  # head/shoulders, centre x=660
        else:
            frames.append([Detection("person", 0.8, 450, 120, 780, 700, 1)])  # body, centre x=615
    assert run_size(CountingEngine([vertical()]), frames, WEBCAM) == []
    assert len(run_size(CountingEngine([vertical()], min_travel_frac=0.0), frames, WEBCAM)) == 2


def test_sitting_down_close_to_the_camera_is_not_a_crossing():
    """Real case: after walking back in, a person 450 px wide sits down; the box
    reshapes and its centre shifts ~110 px back over the line."""
    frames = [[box("person", 4, 963 - 30 * k, 360, size=440)] for k in range(15)]  # walks in: centre 963 -> 543
    frames += [[Detection("person", 0.8, 318 + 8 * k, 100, 768 + 8 * k, 700, 4)] for k in range(15)]  # sits: 543 -> 655
    ev = run_size(CountingEngine([vertical()]), frames, WEBCAM)
    assert [e.direction for e in ev] == ["b_to_a"]  # walking in is counted, sitting down is not


def test_standing_near_the_line_then_leaving_on_the_same_side():
    frames = wander("person", 1, 700, 360, 300, 600, 300, 25)  # 60 px right of the line
    frames += [[box("person", 1, 700 + 20 * k, 360, size=300)] for k in range(30)]  # walks off to the right
    assert run_size(CountingEngine([vertical()]), frames, WEBCAM) == []


def test_small_object_jitter_is_still_absorbed_by_the_band():
    eng = CountingEngine([horizontal()], hysteresis_frac=0.01)
    frames = wander("person", 1, 500, 500, 20, 40, 200, 12, axis=1, step=24)
    assert run(eng, frames) == []


def test_slow_crossing_with_noise_is_counted_once():
    rng = random.Random(3)
    frames = [[box("person", 1, 500, 380 + 0.5 * k + rng.uniform(-3, 3), size=40)] for k in range(500)]
    ev = run(CountingEngine([horizontal()]), frames)
    assert [e.direction for e in ev] == ["b_to_a"]


def test_large_object_walking_slowly_across_is_counted():
    """A person close to the camera (300 px wide) walking across at 3 px/frame."""
    frames = [[box("person", 1, 300 + 3 * k, 360, size=300)] for k in range(230)]
    assert len(run_size(CountingEngine([vertical()]), frames, WEBCAM)) == 1


def test_walk_across_and_back_counts_both_directions():
    frames = [[box("person", 1, 300 + 12 * k, 360, size=250)] for k in range(60)]  # 300 -> 1008
    frames += [[box("person", 1, 1008 - 12 * k, 360, size=250)] for k in range(60)]  # back to 300
    ev = run_size(CountingEngine([vertical()]), frames, WEBCAM)
    assert [e.direction for e in ev] == ["a_to_b", "b_to_a"]  # side A is left of a top-to-bottom line


def test_car_approaching_the_camera_is_counted():
    """Real case (street_cebu, track 335): the box grows from 121x81 to 582x364 px
    while its centre moves from 79 px above a horizontal line to 74 px below it."""
    line = CountingLine("h", (0.1, 0.5), (0.9, 0.5), "both", True)
    frames = []
    for k in range(42):
        h = 81 + 283 * (k / 41) ** 2
        cy = 360 - 79 + 153 * k / 41
        cx = 182 + 800 * k / 41
        frames.append([Detection("car", 0.9, cx - 0.75 * h, cy - h / 2, cx + 0.75 * h, cy + h / 2, 335)])
    ev = run_size(CountingEngine([line]), frames, WEBCAM)
    assert [e.direction for e in ev] == ["b_to_a"]


def test_car_driving_away_is_counted():
    """Real case (person-bicycle-car, track 7): box height 216 -> 112 px while its
    centre moves from +69 to -53 px across a horizontal line in a 768x432 frame."""
    line = CountingLine("h", (0.0, 0.5), (1.0, 0.5), "both", True)
    frames = []
    for k in range(40):
        h = 216 - 104 * k / 39
        cy = 216 + 69 - 122 * min(1.0, k / 30)
        frames.append([Detection("car", 0.9, 300, cy - h / 2, 300 + 1.3 * h, cy + h / 2, 7)])
    ev = run_size(CountingEngine([line]), frames, (768, 432))
    assert [e.direction for e in ev] == ["a_to_b"]


def test_object_appearing_close_to_the_line_is_counted():
    """Real case (person-bicycle-car, track 15): the track starts 25 px from a
    vertical line (box 61 px wide) and rides to 72 px past it."""
    line = CountingLine("v80", (0.8, 0.0), (0.8, 1.0), "both", True)
    x0 = 0.8 * 768
    frames = [[box("bicycle", 15, x0 - 25 + 4 * k, 300, size=60)] for k in range(25)]  # left (A) -> right
    ev = run_size(CountingEngine([line]), frames, (768, 432))
    assert [e.direction for e in ev] == ["a_to_b"]


def test_required_travel_is_capped_for_huge_boxes():
    """A box wider than the frame still counts after 20 % of the diagonal of travel."""
    eng = CountingEngine([vertical()])
    frames = [[Detection("bus", 0.9, x - 700, 100, x + 700, 600, 1)] for x in range(300, 1000, 20)]
    assert len(run_size(eng, frames, WEBCAM)) == 1


def test_bottom_center_anchor_uses_the_footprint():
    """Feet crossing a floor line: travel is measured against the box's thin side, not its height."""
    eng = CountingEngine([horizontal()], anchor="bottom_center")
    frames = [[Detection("person", 0.9, 480, 300 + 8 * k, 520, 480 + 8 * k, 1)] for k in range(10)]  # 40x180 box
    assert len(run(eng, frames)) == 1


def test_track_id_switch_mid_crossing_is_missed():
    """Known limitation: if the tracker assigns a new ID while the object is inside
    the dead zone, the new ID has no known starting side and the crossing is missed."""
    eng = CountingEngine([horizontal()], min_hits=1)
    frames = path("car", 1, 500, 300, 495, 8) + path("car", 2, 500, 505, 700, 8)
    assert run(eng, frames) == []


def test_recorded_webcam_detections():
    """Real detections (tests/data/webcam_straddle_detections.json): the walk out to
    the right and the walk back in are the only crossings of the vertical line; the
    previous rules counted 4 (two while sitting on the line, two while sitting down)."""
    import json
    from pathlib import Path

    doc = json.loads((Path(__file__).parent / "data" / "webcam_straddle_detections.json").read_text())
    size = tuple(doc["size"])
    frames = [[Detection(c, 0.8, x1, y1, x2, y2, tid) for c, tid, x1, y1, x2, y2 in f] for f in doc["frames"]]
    lines = [vertical(), CountingLine("h", (0.1, 0.5), (0.9, 0.5), "both", True)]
    eng = CountingEngine(lines)
    run_size(eng, frames, size)
    assert eng.line_totals["v"] == {"a_to_b": {**eng.line_totals["v"]["a_to_b"], "person": 1},
                                    "b_to_a": {**eng.line_totals["v"]["b_to_a"], "person": 1}}
    assert sum(eng.line_totals["h"]["a_to_b"].values()) + sum(eng.line_totals["h"]["b_to_a"].values()) == 0
    old = CountingEngine(lines, min_travel_frac=0.0)
    run_size(old, frames, size)
    assert sum(old.crossings.values()) == 4


@pytest.mark.parametrize("kw", [{"min_travel_frac": -0.1}, {"min_travel_frac": 2.5}, {"max_travel_frac": 1.5}])
def test_invalid_travel_settings_rejected(kw):
    with pytest.raises(ValueError):
        CountingEngine([horizontal()], **kw)
