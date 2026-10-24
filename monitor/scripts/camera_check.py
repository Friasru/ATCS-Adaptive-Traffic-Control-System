#!/usr/bin/env python3
"""Validate a physical camera end to end (no GUI).

  1. discovery : list cameras with the platform's capture APIs
  2. probe     : open the camera with each API at 640x480 / 1280x720 / 1920x1080
                 and measure the delivered resolution and frame rate
  3. pipeline  : run the real engine (YOLO + ByteTrack + counting + annotated
                 recording + SQLite) on the camera for --seconds, then check the
                 saved video, the database rows and the CSV/JSON exports

Writes go to the normal project folders (recordings/, data/, exports/), so the
session also appears in the app's History. A JSON report is saved to
data/camera_check/ and printed. Exit code 0 means every check passed.

  python scripts/camera_check.py                     # first camera found, 20 s
  python scripts/camera_check.py --index 1 --backend msmf --seconds 30
  python scripts/camera_check.py --probe-only
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402

paths.configure_third_party_dirs()

import cv2  # noqa: E402

from app.camera.sources import BACKENDS, CameraSource, backend_name, camera_backends, discover_cameras  # noqa: E402
from app.detection.classes import MONITORED_CLASSES  # noqa: E402
from app.services.monitoring import MonitoringEngine, State  # noqa: E402
from app.settings import load_settings  # noqa: E402
from app.storage.export import export_session_csv, export_session_json  # noqa: E402
from app.storage.repository import Repository  # noqa: E402
from app.tracking.counter import CountingLine  # noqa: E402

RESOLUTIONS = ((640, 480), (1280, 720), (1920, 1080))


def probe(index: int, api: int, size: tuple[int, int], seconds: float) -> dict:
    row: dict = {"backend": backend_name(api), "requested": f"{size[0]}x{size[1]}"}
    t0 = time.monotonic()
    src = CameraSource(index, size[0], size[1], backend=api)
    try:
        src.open()
    except Exception as exc:
        row.update(ok=False, error=str(exc))
        return row
    row["open_s"] = round(time.monotonic() - t0, 2)
    n, shape, t1 = 0, None, time.monotonic()
    try:
        while time.monotonic() - t1 < seconds:
            f = src.read()
            if f is None:
                break
            shape = f.image.shape
            n += 1
    except Exception as exc:
        row["error"] = str(exc)
    finally:
        src.close()
    span = time.monotonic() - t1
    row.update(ok=n > 0 and "error" not in row, frames=n, measured_fps=round(n / span, 1) if span else 0.0,
               reported_fps=src.fps, delivered=f"{shape[1]}x{shape[0]}" if shape else None)
    return row


def run_pipeline(cam: CameraSource, seconds: float, device: str, line: CountingLine,
                 event_log: str | None = None) -> dict:
    s = load_settings()
    s.device = device
    if event_log:
        s.event_log_level = event_log
    s.camera_width, s.camera_height = cam.width, cam.height
    s.lines = [line.to_dict()]
    s.validate()
    repo = Repository(paths.database_path())
    states: list[str] = []
    errors: list[str] = []
    eng = MonitoringEngine(s, repo, on_state=lambda st, m: states.append(f"{st.value}: {m}"), on_error=errors.append)
    out: dict = {"settings": {"model": s.model_name, "device": device, "imgsz": s.imgsz,
                              "camera": f"{cam.width}x{cam.height}", "line": line.to_dict()}}
    eng.start(cam)
    deadline = time.monotonic() + 120  # first start loads the model
    while eng.state not in (State.DETECTING, State.ERROR) and time.monotonic() < deadline:
        time.sleep(0.1)
    if eng.state != State.DETECTING:
        eng.stop(wait=True)
        repo.close()
        out.update(states=states, errors=errors or [f"engine did not reach Detecting (state {eng.state.value})"])
        return out
    eng.start_recording()
    peak_visible = {c: 0 for c in MONITORED_CLASSES}
    samples = []
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end and eng.is_running:
        time.sleep(1.0)
        sn = eng.latest_snapshot()
        for c in MONITORED_CLASSES:
            peak_visible[c] = max(peak_visible[c], int(sn.visible.get(c, 0)))
        samples.append({"capture_fps": round(sn.capture_fps, 1), "inference_fps": round(sn.inference_fps, 1),
                        "latency_ms": round(sn.latency_ms, 1), "tracks": sn.active_tracks,
                        "frames_dropped": sn.frames_dropped, "device": sn.device})
    eng.stop_recording()
    time.sleep(0.5)
    eng.stop(wait=True, timeout=60)
    eng.join(60)
    rep, rec = eng.last_report, eng.last_recording
    out.update(states=states, errors=errors, samples=samples, peak_visible=peak_visible,
               device=eng.detector.device if eng.detector else None, camera_backend=backend_name(cam.backend or 0))
    if rep is None:
        repo.close()
        return out
    out["session_id"] = rep.session_id
    out["status"] = rep.status
    out["stats"] = rep.stats
    out["recording"] = rec.to_dict() if rec else None
    sess = repo.get_session(rep.session_id)
    events = repo.crossing_events(rep.session_id)
    exp_dir = paths.exports_dir()
    j = export_session_json(repo, rep.session_id, exp_dir / f"session_{rep.session_id[:8]}.json")
    summary_csv, events_csv = export_session_csv(repo, rep.session_id, exp_dir)
    repo.close()
    out["exports"] = [str(j), str(summary_csv), str(events_csv)]
    out["checks"] = verify(sess, events, rec, j, summary_csv, events_csv)
    return out


def verify(sess, events, rec, json_path: Path, summary_csv: Path, events_csv: Path) -> dict:
    """Independent re-reads of what was saved."""
    c: dict = {}
    c["db_session_saved"] = sess is not None and sess["status"] == "completed"
    c["db_source_is_camera"] = bool(sess) and sess["source_type"] == "camera"
    c["db_stats_saved"] = bool(sess) and sess["stats"] is not None
    c["db_recording_row_ok"] = bool(sess) and len(sess["recordings"]) == 1 and sess["recordings"][0]["ok"]
    if rec is not None:
        p = Path(rec.path)
        c["recording_file_exists"] = p.is_file() and p.stat().st_size > 0
        c["recording_inside_recordings_dir"] = p.resolve().parent == paths.recordings_dir().resolve()
        cap = cv2.VideoCapture(str(p))
        n = 0
        while True:
            if not cap.grab():
                break
            n += 1
        cap.release()
        c["recording_frame_count_matches"] = abs(n - rec.frames_written) <= max(2, int(0.01 * rec.frames_written))
        c["recording_validation_ok"] = bool(rec.ok)
    else:
        c["recording_file_exists"] = False
    doc = json.loads(json_path.read_text(encoding="utf-8"))
    st = sess["stats"] if sess and sess["stats"] else {}
    c["json_matches_db"] = (doc["session_id"] == (sess or {}).get("session_id")
                            and all(doc["unique"][k] == int(st.get("unique", {}).get(k, 0)) for k in MONITORED_CLASSES)
                            and all(doc["crossings"][k] == int(st.get("crossings", {}).get(k, 0)) for k in MONITORED_CLASSES)
                            and len(doc["crossing_events"]) == len(events))
    with summary_csv.open(newline="", encoding="utf-8") as fh:
        rows = {r["metric"]: r for r in csv.DictReader(fh)}
    c["summary_csv_matches_db"] = all(
        int(rows[m][k]) == int(st.get(m, {}).get(k, 0)) for m in ("unique", "crossings") for k in MONITORED_CLASSES)
    with events_csv.open(newline="", encoding="utf-8") as fh:
        c["events_csv_matches_db"] = len(list(csv.DictReader(fh))) == len(events)
    c["crossings_equal_event_count"] = sum(int(v) for v in st.get("crossings", {}).values()) == len(events)
    c["all_ok"] = all(v for v in c.values())
    return c


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", type=int, default=None, help="camera index (default: first one discovered)")
    ap.add_argument("--backend", choices=sorted(BACKENDS), default=None, help="capture API (default: discovered)")
    ap.add_argument("--seconds", type=float, default=20.0, help="pipeline run time")
    ap.add_argument("--probe-seconds", type=float, default=3.0)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "gpu"])
    ap.add_argument("--line", default="0.5,0.0,0.5,1.0", help="counting line x1,y1,x2,y2 (normalised); default vertical")
    ap.add_argument("--event-log", choices=["none", "tracks", "all"], default=None,
                    help="event detail stored in SQLite; 'all' keeps every detection so the counting can be replayed")
    ap.add_argument("--probe-only", action="store_true")
    a = ap.parse_args()

    report: dict = {"started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "platform": platform.platform(), "python": platform.python_version(), "opencv": cv2.__version__,
                    "backends_tried": [backend_name(b) for b in camera_backends()]}
    t0 = time.monotonic()
    cams = discover_cameras()
    report["discovery_s"] = round(time.monotonic() - t0, 2)
    report["cameras"] = [{"index": c.index, "name": c.name, "backend": backend_name(c.backend)} for c in cams]
    print(f"discovered in {report['discovery_s']} s: {report['cameras'] or 'no camera'}")

    if a.index is None and not cams:
        report["result"] = "no camera found"
        return finish(report, 2)
    cam_info = next((c for c in cams if a.index is None or c.index == a.index), None)
    index = a.index if a.index is not None else cams[0].index
    name = cam_info.name if cam_info else f"Camera {index}"
    apis = [BACKENDS[a.backend]] if a.backend else camera_backends()

    report["probe"] = []
    for api in apis:
        for size in RESOLUTIONS:
            row = probe(index, api, size, a.probe_seconds)
            report["probe"].append(row)
            print(json.dumps(row))
    if a.probe_only:
        report["result"] = "probe only"
        return finish(report, 0 if any(r["ok"] for r in report["probe"]) else 1)

    api = BACKENDS[a.backend] if a.backend else (cam_info.backend if cam_info else None)
    x1, y1, x2, y2 = (float(v) for v in a.line.split(","))
    cam = CameraSource(index, a.width, a.height, name=name, backend=api)
    print(f"running the pipeline on '{name}' (#{index}) for {a.seconds:.0f} s ...")
    report["pipeline"] = run_pipeline(cam, a.seconds, a.device, CountingLine("line-1", (x1, y1), (x2, y2), "both", True),
                                      a.event_log)
    checks = report["pipeline"].get("checks", {})
    report["result"] = "pass" if checks.get("all_ok") and not report["pipeline"]["errors"] else "fail"
    return finish(report, 0 if report["result"] == "pass" else 1)


def finish(report: dict, rc: int) -> int:
    out_dir = paths.data_dir() / "camera_check"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"camera_check_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "probe"}, indent=2, default=str))
    print(f"\nresult: {report.get('result')}   report: {path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
