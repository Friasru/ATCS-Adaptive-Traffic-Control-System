#!/usr/bin/env python3
"""Headless end-to-end run of the real pipeline on a video file.

Video -> YOLO detection -> ByteTrack -> counting -> annotation -> recording ->
SQLite -> CSV/JSON export. Prints a JSON report.

Example:
  .venv/bin/python scripts/run_video_pipeline.py data/test_media/car-detection.mp4 \
      --line 0.0,0.5,1.0,0.5 --record
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402

paths.configure_third_party_dirs()

from app.camera.sources import VideoFileSource  # noqa: E402
from app.services.monitoring import MonitoringEngine, State  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.storage.export import export_session_csv, export_session_json  # noqa: E402
from app.storage.repository import Repository  # noqa: E402
from app.tracking.counter import CountingLine  # noqa: E402


def parse_line(text: str) -> CountingLine:
    x1, y1, x2, y2 = (float(v) for v in text.split(","))
    return CountingLine("line-1", (x1, y1), (x2, y2), "both", True)


def run(video: Path, settings: Settings, record: bool, db: Path, out_dir: Path, realtime: bool = False,
        record_from_start: bool = True) -> dict:
    repo = Repository(db)
    states: list[str] = []
    errors: list[str] = []
    eng = MonitoringEngine(settings, repo, on_state=lambda s, m: states.append(f"{s.value}: {m}"),
                           on_error=errors.append)
    t0 = time.monotonic()
    eng.start(VideoFileSource(video, realtime=realtime))
    if record and record_from_start:
        eng.start_recording()
    peak_rss = 0
    try:
        import psutil

        proc = psutil.Process()
        proc.cpu_percent(None)
    except ImportError:  # pragma: no cover
        proc = None
    cpu_samples = []
    while eng.is_running:
        time.sleep(0.25)
        if proc is not None:
            peak_rss = max(peak_rss, proc.memory_info().rss)
            cpu_samples.append(proc.cpu_percent(None))
    eng.join()
    wall = time.monotonic() - t0
    rep = eng.last_report
    out: dict = {"video": str(video), "wall_s": round(wall, 2), "states": states, "errors": errors,
                 "final_state": eng.state.value}
    if rep is not None:
        out["session_id"] = rep.session_id
        out["status"] = rep.status
        out["stats"] = rep.stats
        j = export_session_json(repo, rep.session_id, out_dir / f"session_{rep.session_id[:8]}.json")
        c = export_session_csv(repo, rep.session_id, out_dir)
        out["exports"] = [str(j), *map(str, c)]
        sess = repo.get_session(rep.session_id)
        out["recordings"] = sess["recordings"] if sess else []
        out["crossing_events"] = repo.crossing_events(rep.session_id)
    if eng.last_recording is not None:
        out["recording_result"] = eng.last_recording.to_dict()
    out["peak_rss_mb"] = round(peak_rss / 1e6, 1)
    out["avg_cpu_percent"] = round(sum(cpu_samples) / len(cpu_samples), 1) if cpu_samples else None
    out["device"] = eng.detector.device if eng.detector else None
    repo.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--line", default="0.0,0.5,1.0,0.5", help="x1,y1,x2,y2 normalised")
    ap.add_argument("--record", action="store_true")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "gpu"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--imgsz", type=int, default=None)
    ap.add_argument("--conf", type=float, default=None)
    ap.add_argument("--anchor", default=None, choices=["center", "bottom_center"])
    ap.add_argument("--realtime", action="store_true", help="pace the file at its native FPS")
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--quiet-events", action="store_true")
    a = ap.parse_args()

    s = Settings()
    s.device = a.device
    if a.model:
        s.model_name = a.model
    if a.imgsz is not None:
        s.imgsz = a.imgsz
    if a.conf:
        s.confidence = a.conf
    if a.anchor:
        s.anchor = a.anchor
    s.lines = [parse_line(a.line).to_dict()]
    s.validate()
    report = run(a.video, s, a.record, a.db or paths.database_path(), a.out or paths.exports_dir(), a.realtime)
    if a.quiet_events:
        report.pop("crossing_events", None)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("status") == "completed" and not report["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
