"""Session / event persistence on top of SQLite.

All writes go through one connection guarded by a lock and run inside explicit
transactions. ``EventWriter`` batches high-frequency events on a background
thread so the processing pipeline never waits for disk I/O.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.storage.database import connect, init_schema

log = logging.getLogger(__name__)


def iso(ts: Optional[float] = None) -> str:
    """ISO 8601 with explicit UTC offset."""
    dt = datetime.now(timezone.utc) if ts is None else datetime.fromtimestamp(ts, timezone.utc)
    return dt.isoformat(timespec="milliseconds")


class StorageError(RuntimeError):
    pass


class Repository:
    def __init__(self, path: Path | str):
        self.path = str(path)
        try:
            self._conn = connect(self.path)
            init_schema(self._conn)
        except sqlite3.Error as exc:
            raise StorageError(f"Cannot open database {self.path}: {exc}") from exc
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # -------------------------------------------------------------- sessions
    def mark_interrupted_sessions(self) -> int:
        """Sessions left 'running' by a crash or kill are marked 'interrupted'."""
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE sessions SET status='interrupted', ended_at=COALESCE(ended_at, ?), "
                "error=COALESCE(error, 'Application ended unexpectedly') WHERE status='running'",
                (iso(),),
            )
            c.execute(
                "UPDATE recordings SET error=COALESCE(error, 'Recording not finalized (application ended unexpectedly)') "
                "WHERE ended_at IS NULL"
            )
            return cur.rowcount

    def create_session(self, session_id: str, source_type: str, source_id: str, model_name: str,
                       model_version: str, device: str, settings: dict, started_at: Optional[str] = None) -> None:
        with self.transaction() as c:
            c.execute(
                "INSERT INTO sessions(session_id, source_type, source_id, started_at, status, model_name, "
                "model_version, device, settings_json) VALUES (?,?,?,?, 'running', ?,?,?,?)",
                (session_id, source_type, source_id, started_at or iso(), model_name, model_version, device,
                 json.dumps(settings, sort_keys=True)),
            )

    def finish_session(self, session_id: str, status: str, stats: dict, error: Optional[str] = None,
                       ended_at: Optional[str] = None) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE sessions SET status=?, ended_at=?, error=? WHERE session_id=?",
                (status, ended_at or iso(), error, session_id),
            )
            c.execute(
                "INSERT OR REPLACE INTO session_stats(session_id, unique_json, crossings_json, line_totals_json, "
                "duration_s, frames_processed, avg_inference_fps, avg_capture_fps, peak_visible, avg_visible, "
                "recording_duration_s) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    json.dumps(stats["unique"]),
                    json.dumps(stats["crossings"]),
                    json.dumps(stats.get("line_totals", {})),
                    float(stats.get("duration_s", 0.0)),
                    int(stats.get("frames_processed", 0)),
                    stats.get("avg_inference_fps"),
                    stats.get("avg_capture_fps"),
                    stats.get("peak_visible"),
                    stats.get("avg_visible"),
                    float(stats.get("recording_duration_s", 0.0)),
                ),
            )

    def add_recording(self, session_id: str, path: str, codec: str, container: str, started_at: str) -> int:
        with self.transaction() as c:
            cur = c.execute(
                "INSERT INTO recordings(session_id, path, codec, container, started_at) VALUES (?,?,?,?,?)",
                (session_id, path, codec, container, started_at),
            )
            return int(cur.lastrowid or 0)

    def finish_recording(self, rec_id: int, result: dict) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE recordings SET ended_at=?, frames=?, duration_s=?, ok=?, error=?, info_json=? WHERE id=?",
                (result.get("ended_at") or iso(), result.get("frames_written"), result.get("duration_s"),
                 1 if result.get("ok") else 0, result.get("error"), json.dumps(result, default=str), rec_id),
            )

    def insert_events(self, crossings: Iterable[tuple], detections: Iterable[tuple]) -> None:
        crossings, detections = list(crossings), list(detections)
        if not crossings and not detections:
            return
        with self.transaction() as c:
            if crossings:
                c.executemany(
                    "INSERT INTO crossing_events(session_id, ts, line_id, track_id, cls, direction) VALUES (?,?,?,?,?,?)",
                    crossings,
                )
            if detections:
                c.executemany(
                    "INSERT INTO detection_events(session_id, ts, event_type, cls, confidence, track_id, x1, y1, x2, y2) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    detections,
                )

    # ----------------------------------------------------------------- reads
    def _rows(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def list_sessions(self, limit: int = 500) -> list[dict]:
        rows = self._rows(
            "SELECT s.*, st.unique_json, st.crossings_json, st.duration_s, st.frames_processed, "
            "st.avg_inference_fps, st.recording_duration_s, "
            "(SELECT group_concat(path, '|') FROM recordings r WHERE r.session_id=s.session_id) AS recording_paths "
            "FROM sessions s LEFT JOIN session_stats st ON st.session_id=s.session_id "
            "ORDER BY s.started_at DESC LIMIT ?",
            (limit,),
        )
        for r in rows:
            r["unique"] = json.loads(r.pop("unique_json") or "null")
            r["crossings"] = json.loads(r.pop("crossings_json") or "null")
            r["recording_paths"] = [p for p in (r.get("recording_paths") or "").split("|") if p]
        return rows

    def get_session(self, session_id: str) -> Optional[dict]:
        rows = self._rows("SELECT * FROM sessions WHERE session_id=?", (session_id,))
        if not rows:
            return None
        s = rows[0]
        s["settings"] = json.loads(s.pop("settings_json") or "{}")
        st = self._rows("SELECT * FROM session_stats WHERE session_id=?", (session_id,))
        if st:
            stats = st[0]
            stats.pop("session_id")
            stats["unique"] = json.loads(stats.pop("unique_json"))
            stats["crossings"] = json.loads(stats.pop("crossings_json"))
            stats["line_totals"] = json.loads(stats.pop("line_totals_json"))
            s["stats"] = stats
        else:
            s["stats"] = None
        recs = self._rows("SELECT * FROM recordings WHERE session_id=? ORDER BY id", (session_id,))
        for r in recs:
            r["info"] = json.loads(r.pop("info_json") or "null")
            r["ok"] = bool(r["ok"])
        s["recordings"] = recs
        return s

    def crossing_events(self, session_id: str) -> list[dict]:
        return self._rows(
            "SELECT ts, line_id, track_id, cls, direction FROM crossing_events WHERE session_id=? ORDER BY id",
            (session_id,),
        )

    def detection_events(self, session_id: str) -> list[dict]:
        return self._rows(
            "SELECT ts, event_type, cls, confidence, track_id, x1, y1, x2, y2 FROM detection_events "
            "WHERE session_id=? ORDER BY id",
            (session_id,),
        )


class EventWriter(threading.Thread):
    """Background batching writer. The queue is unbounded on purpose: events
    are small, and silently discarding them is not acceptable."""

    def __init__(self, repo: Repository, flush_interval: float = 0.5):
        super().__init__(name="db-writer", daemon=True)
        self.repo = repo
        self.flush_interval = flush_interval
        self._q: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self.error: Optional[str] = None
        self.written = 0
        self.failed = 0

    def add_crossing(self, row: tuple) -> None:
        self._q.put(("c", row))

    def add_detection(self, row: tuple) -> None:
        self._q.put(("d", row))

    def flush_and_stop(self, timeout: float = 15.0) -> None:
        self._stop_evt.set()
        self.join(timeout)

    def run(self) -> None:
        pending_c: list = []
        pending_d: list = []
        while True:
            stopping = self._stop_evt.is_set()
            try:
                kind, row = self._q.get(timeout=self.flush_interval)
                (pending_c if kind == "c" else pending_d).append(row)
                while True:
                    kind, row = self._q.get_nowait()
                    (pending_c if kind == "c" else pending_d).append(row)
            except queue.Empty:
                pass
            if pending_c or pending_d:
                for attempt in range(3):
                    try:
                        self.repo.insert_events(pending_c, pending_d)
                        self.written += len(pending_c) + len(pending_d)
                        pending_c, pending_d = [], []
                        break
                    except sqlite3.Error as exc:
                        self.error = f"Database write failed: {exc}"
                        log.error("%s (attempt %d)", self.error, attempt + 1)
                        self._stop_evt.wait(0.2)
                else:
                    if stopping:
                        self.failed += len(pending_c) + len(pending_d)
                        log.error("Gave up writing %d events", self.failed)
                        pending_c, pending_d = [], []
            if stopping and self._q.empty() and not pending_c and not pending_d:
                return
