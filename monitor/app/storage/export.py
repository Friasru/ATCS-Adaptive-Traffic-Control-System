"""CSV / JSON export of saved sessions."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from app.detection.classes import MONITORED_CLASSES
from app.integration.schemas import session_summary
from app.storage.repository import Repository, StorageError

CROSSING_COLUMNS = ("session_id", "timestamp", "line_id", "track_id", "class", "direction")
SUMMARY_COLUMNS = ("session_id", "source_type", "source_id", "started_at", "ended_at", "status", "metric",
                   *MONITORED_CLASSES, "total")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in untrusted text fields."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _load(repo: Repository, session_id: str) -> dict:
    s = repo.get_session(session_id)
    if s is None:
        raise StorageError(f"Unknown session {session_id}")
    return s


def export_session_json(repo: Repository, session_id: str, out_path: Path) -> Path:
    s = _load(repo, session_id)
    doc = session_summary(s, repo.crossing_events(session_id))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out_path)
    return out_path


def export_session_csv(repo: Repository, session_id: str, out_dir: Path) -> tuple[Path, Path]:
    """Write <id>_summary.csv (per-class totals) and <id>_crossings.csv (events)."""
    s = _load(repo, session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"session_{session_id[:8]}"
    summary_path = out_dir / f"{stem}_summary.csv"
    events_path = out_dir / f"{stem}_crossings.csv"
    stats = s.get("stats") or {}
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(SUMMARY_COLUMNS)
        for metric in ("unique", "crossings"):
            counts = stats.get(metric) or {}
            vals = [int(counts.get(c, 0)) for c in MONITORED_CLASSES]
            w.writerow([csv_safe(x) for x in (s["session_id"], s["source_type"], s["source_id"], s["started_at"],
                        s["ended_at"] or "", s["status"], metric)] + vals + [sum(vals)])
    with events_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CROSSING_COLUMNS)
        for e in repo.crossing_events(session_id):
            w.writerow([csv_safe(session_id), e["ts"], csv_safe(e["line_id"]), e["track_id"], e["cls"], e["direction"]])
    return summary_path, events_path
