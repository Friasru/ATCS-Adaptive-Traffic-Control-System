"""Real YOLO inference + tracking + full pipeline on real video files.

These tests load the actual weights from models/ and decode real videos from
data/test_media/ (skipped with a clear reason when either is missing).
Executing without errors is not taken as proof of accuracy; accuracy against
hand-labelled ground truth is measured by scripts/evaluate_counts.py.
"""

import json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import pytest
import ultralytics

from app.camera.sources import VideoFileSource
from app.detection.classes import MONITORED_CLASSES
from app.detection.detector import Detector
from app.integration.schemas import validate_session_summary
from app.services.monitoring import MonitoringEngine, State
from app.settings import Settings
from app.storage.export import export_session_csv, export_session_json
from app.storage.repository import Repository
from tests.conftest import DEFAULT_MODEL, MODELS, require_media, require_model

pytestmark = pytest.mark.model
BUS = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"


@pytest.fixture(scope="module")
def detector():
    require_model()
    d = Detector(DEFAULT_MODEL, "auto", models_dir=MODELS)
    d.load()
    return d


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def test_model_class_mapping_matches_coco(detector):
    names = detector.model.names
    for cls, idx in detector.class_ids.items():
        assert names[idx] == cls
    assert len(set(detector.class_ids.values())) == 6


def test_inference_on_sample_image(detector):
    img = cv2.imread(str(BUS))
    dets = detector.detect(img, 0.35, 0.5, 640)
    h, w = img.shape[:2]
    assert dets, "no detections on bus.jpg"
    assert all(d.cls in MONITORED_CLASSES for d in dets)
    assert all(d.is_valid(w, h) for d in dets)
    found = {d.cls for d in dets}
    assert {"bus", "person"} <= found  # sanity check on a well-known image


def test_empty_frame_gives_no_detections(detector):
    assert detector.detect(np.zeros((480, 640, 3), np.uint8), 0.35, 0.5, 640) == []
    detector.reset_tracker()
    assert detector.track(np.zeros((480, 640, 3), np.uint8), 0.35, 0.5, 640) == []


def test_cpu_inference_path():
    require_model()
    d = Detector(DEFAULT_MODEL, "cpu", models_dir=MODELS)
    d.load()
    assert d.device == "cpu"
    assert d.detect(cv2.imread(str(BUS)), 0.35, 0.5, 320)


def test_tracking_ids_persist_across_real_frames(detector):
    video = require_media("person-bicycle-car-detection.mp4")
    detector.reset_tracker()
    cap = cv2.VideoCapture(str(video))
    boxes = defaultdict(list)  # track_id -> [(frame, box)]
    for k in range(240):
        ok, img = cap.read()
        if not ok:
            break
        for d in detector.track(img, 0.35, 0.5, 640):
            if d.track_id is not None:
                boxes[d.track_id].append((k, (d.x1, d.y1, d.x2, d.y2)))
    cap.release()
    assert boxes, "tracker produced no IDs"
    longest = max(boxes.values(), key=len)
    assert len(longest) >= 20, "no track survived 20 frames"
    # Consecutive boxes of the same ID overlap: IDs follow one object.
    pairs = [(a[1], b[1]) for a, b in zip(longest, longest[1:]) if b[0] - a[0] == 1]
    assert np.mean([iou(a, b) > 0.3 for a, b in pairs]) > 0.9
    detector.reset_tracker()
    cap = cv2.VideoCapture(str(video))
    ids = []
    for _ in range(60):
        ok, img = cap.read()
        ids += [d.track_id for d in detector.track(img, 0.35, 0.5, 640) if d.track_id is not None]
    cap.release()
    assert ids and min(ids) == 1  # tracker reset restarts IDs


@pytest.mark.media
def test_full_pipeline_on_real_video(model_home):
    video = require_media("street_slovenska.mp4")
    s = Settings(model_name=DEFAULT_MODEL)
    s.lines = [{"line_id": "line-1", "p1": [0.55, 0.45], "p2": [0.55, 1.0], "direction": "both", "enabled": True}]
    s.event_log_level = "tracks"
    repo = Repository(model_home / "db.sqlite3")
    errors = []
    eng = MonitoringEngine(s, repo, on_error=errors.append)
    eng.start(VideoFileSource(video, realtime=False))
    eng.start_recording()
    eng.join(600)
    assert errors == [] and eng.state == State.IDLE
    rep = eng.last_report
    total_frames = int(cv2.VideoCapture(str(video)).get(cv2.CAP_PROP_FRAME_COUNT))
    assert rep.status == "completed"
    assert abs(rep.stats["frames_processed"] - total_frames) <= 1
    assert sum(rep.stats["unique"].values()) > 0
    # Stored events agree with the session totals.
    events = repo.crossing_events(rep.session_id)
    by_cls = {c: 0 for c in MONITORED_CLASSES}
    for e in events:
        by_cls[e["cls"]] += 1
    assert by_cls == rep.stats["crossings"]
    assert len({(e["track_id"], e["direction"]) for e in events}) == len(events)  # no duplicates
    # Recording is real and decodable, one frame per processed frame.
    res = eng.last_recording
    assert res.ok, res.error
    assert res.validation["decoded_frames"] == rep.stats["frames_processed"]
    assert res.validation["duration_s"] == pytest.approx(total_frames / 15.0, abs=0.2)
    # Exports open and validate.
    j = export_session_json(repo, rep.session_id, model_home / "exports" / "s.json")
    validate_session_summary(json.loads(j.read_text()))
    summary, crossings = export_session_csv(repo, rep.session_id, model_home / "exports")
    assert summary.read_text().count("\n") == 3
    assert crossings.read_text().count("\n") == len(events) + 1
    repo.close()


@pytest.mark.media
def test_stream_restart_gives_independent_sessions(model_home):
    video = require_media("person-bicycle-car-detection.mp4")
    s = Settings(model_name=DEFAULT_MODEL)
    s.lines = [{"line_id": "line-1", "p1": [0.5, 0.0], "p2": [0.5, 1.0], "direction": "both", "enabled": True}]
    repo = Repository(model_home / "db.sqlite3")
    eng = MonitoringEngine(s, repo)
    reports = []
    for _ in range(2):
        eng.start(VideoFileSource(video, realtime=False))
        eng.join(600)
        reports.append(eng.last_report)
    a, b = (sum(r.stats["crossings"].values()) for r in reports)
    assert reports[0].session_id != reports[1].session_id
    assert abs(a - b) <= 1, "second run must not accumulate the first run's counts"
    repo.close()


def test_ultralytics_analytics_disabled_after_load(detector):
    from ultralytics.utils import SETTINGS
    from ultralytics.utils.events import events

    assert SETTINGS["sync"] is False
    events.__init__()  # re-evaluate the enable condition with current settings
    assert events.enabled is False
