"""Unit tests: geometry, classes, settings, frame buffer, detector conversion."""

import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.camera.manager import EOS, FrameBuffer, RateMeter
from app.detection.classes import MONITORED_CLASSES, resolve_class_ids
from app.detection.detector import Detector
from app.settings import Settings, load_settings, save_settings
from app.tracking.geometry import segments_intersect, signed_distance, valid_normalized_point

COCO = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 4: "airplane", 5: "bus", 6: "train", 7: "truck"}


# ------------------------------------------------------------------ geometry
@pytest.mark.parametrize(
    "p1,p2,q1,q2,expected",
    [
        ((0, 0), (10, 10), (0, 10), (10, 0), True),
        ((0, 0), (1, 1), (2, 2), (3, 3), False),  # collinear, disjoint
        ((0, 0), (2, 2), (1, 1), (3, 3), True),  # collinear overlap
        ((0, 0), (5, 0), (5, 0), (5, 5), True),  # touching end point
        ((0, 0), (4, 0), (5, -1), (5, 1), False),  # stops short
        ((0, 5), (10, 5), (11, 0), (11, 10), False),
    ],
)
def test_segments_intersect(p1, p2, q1, q2, expected):
    assert segments_intersect(p1, p2, q1, q2) is expected


def test_signed_distance_sides_in_image_coordinates():
    a, b = (0, 100), (200, 100)  # left -> right
    assert signed_distance((50, 150), a, b) == pytest.approx(50)  # below = right-hand side = A
    assert signed_distance((50, 40), a, b) == pytest.approx(-60)
    assert signed_distance((5, 5), (1, 1), (1, 1)) == 0.0


def test_valid_normalized_point():
    assert valid_normalized_point((0, 1))
    assert not valid_normalized_point((1.01, 0))
    assert not valid_normalized_point(("a", 0))
    assert not valid_normalized_point((float("inf"), 0))
    assert not valid_normalized_point(None)


# ------------------------------------------------------------------- classes
def test_resolve_class_ids_uses_model_names():
    ids = resolve_class_ids(COCO)
    assert ids == {"person": 0, "car": 2, "motorcycle": 3, "bicycle": 1, "bus": 5, "truck": 7}
    assert ids["motorcycle"] != ids["bicycle"] != ids["car"]


def test_resolve_class_ids_rejects_incomplete_model():
    with pytest.raises(ValueError, match="truck"):
        resolve_class_ids({0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus"})


# ------------------------------------------------------------------ settings
def test_default_settings_valid():
    Settings().validate()


@pytest.mark.parametrize(
    "field,value",
    [
        ("confidence", 0.0), ("confidence", 1.5), ("confidence", float("nan")), ("confidence", "0.5"),
        ("imgsz", 641), ("imgsz", -640), ("imgsz", 64), ("max_processing_fps", -1),
        ("device", "tpu"), ("tracker", "sort"), ("enabled_classes", []), ("enabled_classes", ["cat"]),
        ("camera_width", -640), ("camera_height", 0), ("camera_width", True), ("min_hits", 0),
        ("anchor", "top"), ("hysteresis_frac", 0.5), ("min_travel_frac", 2.5), ("min_travel_frac", -0.1),
        ("event_log_level", "verbose"),
        ("model_name", "../evil.pt"), ("model_name", "model.exe"),
        ("lines", [{"line_id": "a", "p1": [0, 0], "p2": [2, 2]}]),
    ],
)
def test_invalid_settings_rejected(field, value):
    s = Settings()
    setattr(s, field, value)
    with pytest.raises(ValueError):
        s.validate()


def test_settings_roundtrip_and_corrupt_file(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings(confidence=0.5, enabled_classes=["car", "bus"])
    save_settings(s, p)
    assert load_settings(p) == s
    p.write_text("{not json")
    assert load_settings(p) == Settings()  # corrupt -> defaults, no crash
    p.write_text(json.dumps({"confidence": 7}))
    assert load_settings(p) == Settings()  # invalid value -> defaults


def test_save_settings_refuses_invalid(tmp_path):
    with pytest.raises(ValueError):
        save_settings(Settings(confidence=2.0), tmp_path / "s.json")
    assert not (tmp_path / "s.json").exists()


# -------------------------------------------------------------- frame buffer
def test_live_buffer_keeps_only_latest_and_counts_drops():
    b = FrameBuffer(lossless=False)
    stop = threading.Event()
    for i in range(5):
        b.put(i, stop)
    assert len(b) == 1 and b.dropped == 4
    assert b.get(0.1) == 4


def test_lossless_buffer_blocks_instead_of_dropping():
    b = FrameBuffer(lossless=True, capacity=2)
    stop = threading.Event()
    b.put(1, stop)
    b.put(2, stop)
    t = threading.Thread(target=b.put, args=(3, stop))
    t.start()
    time.sleep(0.15)
    assert t.is_alive()  # blocked: queue full
    assert b.get(0.1) == 1
    t.join(1)
    assert [b.get(0.1), b.get(0.1)] == [2, 3] and b.dropped == 0


def test_eos_never_dropped_and_stop_unblocks():
    b = FrameBuffer(lossless=False)
    stop = threading.Event()
    b.put(1, stop)
    b.put(EOS, stop)
    assert b.get(0.1) == 1 and b.get(0.1) is EOS
    lb = FrameBuffer(lossless=True, capacity=1)
    lb.put(1, stop)
    stop.set()
    assert lb.put(2, stop) is False


def test_rate_meter():
    m = RateMeter(window=10)
    for i in range(11):
        m.tick(time.monotonic() - 1.0 + i * 0.1)
    assert m.rate == pytest.approx(10, rel=0.05)


# ------------------------------------------------------ detector conversion
class _T:
    def __init__(self, a):
        self.a = np.asarray(a)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


def fake_result(xyxy, conf, cls, ids=None):
    boxes = SimpleNamespace(xyxy=_T(xyxy), conf=_T(conf), cls=_T(cls), id=_T(ids) if ids is not None else None)
    boxes.__len__ = lambda: len(conf)
    return SimpleNamespace(boxes=_Boxes(boxes))


class _Boxes:
    def __init__(self, ns):
        self.__dict__.update(ns.__dict__)

    def __len__(self):
        return len(self.conf.a)


def make_detector():
    d = Detector("yolo26n.pt")
    d.class_ids = resolve_class_ids(COCO)
    d.id_to_class = {v: k for k, v in d.class_ids.items()}
    return d


def test_convert_maps_classes_ids_and_clips():
    d = make_detector()
    r = fake_result(
        [[10, 10, 50, 50], [-5, 0, 700, 30], [0, 0, 5, 5], [1, 1, 1, 9]],
        [0.9, 0.5, 0.7, 0.8],
        [3, 2, 4, 0],  # motorcycle, car, airplane (ignored), degenerate person
        [11, 12, 13, 14],
    )
    dets = d.convert(r, 640, 480)
    assert [(x.cls, x.track_id) for x in dets] == [("motorcycle", 11), ("car", 12)]
    assert dets[1].x1 == 0 and dets[1].x2 == 640  # clipped into the frame
    assert all(x.is_valid(640, 480) for x in dets)


def test_convert_without_track_ids_and_empty():
    d = make_detector()
    dets = d.convert(fake_result([[1, 1, 9, 9]], [0.4], [1]), 100, 100)
    assert dets[0].cls == "bicycle" and dets[0].track_id is None
    assert d.convert(SimpleNamespace(boxes=None), 10, 10) == []


def test_monitored_classes_are_the_six_required():
    assert set(MONITORED_CLASSES) == {"person", "car", "motorcycle", "bicycle", "bus", "truck"}


def test_auto_inference_size_depends_on_device():
    s = Settings()
    assert s.imgsz == 0
    assert s.effective_imgsz("cuda:0") == 960 and s.effective_imgsz("cpu") == 640
    s.imgsz = 480
    assert s.effective_imgsz("cuda:0") == 480
    s.validate()


def test_tracked_filter_keeps_established_tracks_through_low_scores():
    from app.detection.types import Detection

    d = make_detector()
    low = Detection("car", 0.15, 0, 0, 10, 10, 7)
    high = Detection("car", 0.6, 0, 0, 10, 10, 7)
    assert d.filter_tracked([low], 0.35) == []  # never reached the threshold: hidden
    assert d.filter_tracked([high], 0.35) == [high]
    assert d.filter_tracked([low], 0.35) == [low]  # same track, now kept alive
    assert d.filter_tracked([Detection("car", 0.2, 0, 0, 5, 5, None)], 0.35) == []
    d._established.clear()
    assert d.filter_tracked([low], 0.35) == []
