#!/usr/bin/env python3
"""Compare the app's line-crossing counts with hand-labelled ground truth.

Runs the real engine (YOLO + ByteTrack + counting) on each video listed in
tests/data/ground_truth.json and reports per line / direction / class the
expected vs. counted values. Classes not listed in "labelled_classes" for a
video are reported but excluded from scoring.

  .venv/bin/python scripts/evaluate_counts.py [--device cpu] [--model yolo26n.pt]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import paths  # noqa: E402

paths.configure_third_party_dirs()

from app.camera.sources import VideoFileSource  # noqa: E402
from app.detection.classes import MONITORED_CLASSES  # noqa: E402
from app.services.monitoring import MonitoringEngine  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.storage.repository import Repository  # noqa: E402

GT = ROOT / "tests" / "data" / "ground_truth.json"


def evaluate(video: Path, spec: dict, settings: Settings, db: Path) -> dict:
    s = settings.copy()
    s.lines = spec["lines"]
    s.validate()
    repo = Repository(db)
    eng = MonitoringEngine(s, repo)
    eng.start(VideoFileSource(video, realtime=False))
    eng.join(1800)
    rep = eng.last_report
    repo.close()
    labelled = set(spec.get("labelled_classes", MONITORED_CLASSES))
    rows, tp, fp, fn = [], 0, 0, 0
    for lid, dirs in spec["expected"].items():
        got_line = rep.stats["line_totals"][lid]
        for direction, exp in dirs.items():
            for cls in MONITORED_CLASSES:
                e, g = exp.get(cls, 0), got_line[direction][cls]
                scored = cls in labelled
                if scored:
                    tp += min(e, g)
                    fp += max(0, g - e)
                    fn += max(0, e - g)
                if e or g:
                    rows.append({"line": lid, "direction": direction, "class": cls, "expected": e if scored else None,
                                 "counted": g, "scored": scored})
    return {"video": video.name, "status": rep.status, "frames": rep.stats["frames_processed"],
            "unique": rep.stats["unique"], "rows": rows, "tp": tp, "fp": fp, "fn": fn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "gpu"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--imgsz", type=int, default=None)
    ap.add_argument("--anchor", default=None, choices=["center", "bottom_center"])
    ap.add_argument("--json", type=Path, default=None, help="also write the report to this file")
    a = ap.parse_args()
    s = Settings(device=a.device)
    if a.model:
        s.model_name = a.model
    if a.imgsz is not None:
        s.imgsz = a.imgsz
    if a.anchor:
        s.anchor = a.anchor
    gt = json.loads(GT.read_text())
    media = paths.project_root() / "data" / "test_media"
    reports = []
    with tempfile.TemporaryDirectory(dir=paths.data_dir()) as tmp:
        for name, spec in gt["videos"].items():
            video = media / name
            if not video.is_file():
                print(f"SKIP {name}: missing (run scripts/fetch_test_media.sh)")
                continue
            r = evaluate(video, spec, s, Path(tmp) / f"{video.stem}.sqlite3")
            reports.append(r)
            print(f"\n== {name}  ({s.model_name}, imgsz {s.imgsz or "auto"}, anchor {s.anchor}, {r['frames']} frames)")
            print(f"{'line':<6}{'direction':<9}{'class':<12}{'expected':>9}{'counted':>9}")
            for row in r["rows"]:
                exp = "-" if row["expected"] is None else row["expected"]
                print(f"{row['line']:<6}{row['direction']:<9}{row['class']:<12}{exp:>9}{row['counted']:>9}"
                      + ("" if row["scored"] else "   (not labelled)"))
            print(f"scored: TP={r['tp']} FP={r['fp']} FN={r['fn']}  precision={r['precision']}  recall={r['recall']}")
    tp = sum(r["tp"] for r in reports)
    fp = sum(r["fp"] for r in reports)
    fn = sum(r["fn"] for r in reports)
    print(f"\nOVERALL: TP={tp} FP={fp} FN={fn}  precision={tp / (tp + fp) if tp + fp else 0:.3f}  "
          f"recall={tp / (tp + fn) if tp + fn else 0:.3f}")
    if a.json:
        a.json.write_text(json.dumps({"settings": s.to_dict(), "reports": reports}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
