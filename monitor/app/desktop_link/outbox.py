"""Durable outbox of finished sessions waiting for upload.

A separate SQLite file (desktop_link/outbox.sqlite3); the app's own history
database is only read. One row per session the link has seen:

  open     - seen while running; not finished locally yet (or a crash left it
             'running' until the next start marks it interrupted)
  pending  - finished and frozen: the exact event list and summary to send
  done     - acknowledged by the backend; the payload is deleted, a small
             record stays so the session is never sent twice
  rejected - refused permanently by the backend (409/422/400...). The payload
             is kept and the reason is shown by `python -m app.desktop_link
             status`; `retry` puts it back in the queue. Never deleted silently.

The payload is frozen once, so retries always send identical documents (a
changed summary would be refused as a conflict). Upload progress (events_sent)
is saved after every page, so a restart resumes where it stopped.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.desktop_link.documents import FrozenSession

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS uploads (
    session_id       TEXT PRIMARY KEY,
    state            TEXT NOT NULL CHECK (state IN ('open', 'pending', 'done', 'rejected')),
    first_seen_at    REAL NOT NULL,
    frozen_at        REAL,
    envelope_json    TEXT,
    events_json      TEXT,
    summary_json     TEXT,
    events_total     INTEGER NOT NULL DEFAULT 0,
    events_sent      INTEGER NOT NULL DEFAULT 0,
    events_resends   INTEGER NOT NULL DEFAULT 0,
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  REAL NOT NULL DEFAULT 0,
    last_code        TEXT,
    last_error       TEXT,
    finished_at      REAL,
    result_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_uploads_state ON uploads(state, next_attempt_at);
"""


@dataclass
class Item:
    session_id: str
    state: str
    envelope: dict
    events: list
    summary: dict
    events_total: int
    events_sent: int
    events_resends: int
    attempts: int


class Outbox:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")  # takes effect for a new file
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")  # acknowledged state must survive a power cut
        self._conn.executescript(SCHEMA)
        # A new process starts with fresh backoff: the backend may be back.
        self._conn.execute("UPDATE uploads SET next_attempt_at = 0 WHERE state = 'pending'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, args)

    # ------------------------------------------------------------- writes
    def observe(self, session_id: str, now: Optional[float] = None) -> bool:
        """Remember a session seen by this app. True if it was new."""
        cur = self._exec("INSERT OR IGNORE INTO uploads(session_id, state, first_seen_at) VALUES (?, 'open', ?)",
                         (session_id, now or time.time()))
        return cur.rowcount == 1

    def freeze(self, frozen: FrozenSession, now: Optional[float] = None) -> bool:
        """Store the final payload of an open session. False if it was already frozen."""
        cur = self._exec(
            "UPDATE uploads SET state='pending', frozen_at=?, envelope_json=?, events_json=?, summary_json=?, "
            "events_total=?, events_sent=0, next_attempt_at=0 WHERE session_id=? AND state='open'",
            (now or time.time(), json.dumps(frozen.envelope), json.dumps(frozen.events), json.dumps(frozen.summary),
             len(frozen.events), frozen.session_id))
        return cur.rowcount == 1

    def events_progress(self, session_id: str, sent: int) -> None:
        self._exec("UPDATE uploads SET events_sent=?, attempts=0, last_code=NULL, last_error=NULL "
                   "WHERE session_id=? AND state='pending'", (sent, session_id))

    def resend_events(self, session_id: str) -> None:
        self._exec("UPDATE uploads SET events_sent=0, events_resends=events_resends+1, next_attempt_at=0 "
                   "WHERE session_id=? AND state='pending'", (session_id,))

    def done(self, session_id: str, result: dict, now: Optional[float] = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE uploads SET state='done', finished_at=?, result_json=?, envelope_json=NULL, events_json=NULL, "
                "summary_json=NULL, last_code=NULL, last_error=NULL WHERE session_id=? AND state='pending'",
                (now or time.time(), json.dumps(result)[:2000], session_id))
            self._conn.execute("PRAGMA incremental_vacuum")

    def failed(self, session_id: str, code: str, message: str, retry_at: float) -> None:
        self._exec("UPDATE uploads SET attempts=attempts+1, next_attempt_at=?, last_code=?, last_error=? "
                   "WHERE session_id=? AND state='pending'", (retry_at, code[:64], message[:300], session_id))

    def reject(self, session_id: str, code: str, message: str, now: Optional[float] = None) -> None:
        self._exec("UPDATE uploads SET state='rejected', finished_at=?, last_code=?, last_error=? "
                   "WHERE session_id=? AND state='pending'", (now or time.time(), code[:64], message[:300], session_id))

    def retry_now(self) -> None:
        """The backend is reachable again: skip the remaining backoff of pending uploads."""
        self._exec("UPDATE uploads SET next_attempt_at=0 WHERE state='pending'")

    def retry_rejected(self, session_id: Optional[str] = None) -> int:
        sql = ("UPDATE uploads SET state='pending', attempts=0, events_sent=0, events_resends=0, next_attempt_at=0, "
               "finished_at=NULL WHERE state='rejected'")
        cur = self._exec(sql + (" AND session_id=?" if session_id else ""), (session_id,) if session_id else ())
        return cur.rowcount

    # -------------------------------------------------------------- reads
    def state_of(self, session_id: str) -> Optional[str]:
        row = self._exec("SELECT state FROM uploads WHERE session_id=?", (session_id,)).fetchone()
        return row["state"] if row else None

    def open_sessions(self) -> list[str]:
        return [r["session_id"] for r in self._exec("SELECT session_id FROM uploads WHERE state='open' "
                                                    "ORDER BY first_seen_at").fetchall()]

    def next_due(self, now: Optional[float] = None) -> Optional[Item]:
        row = self._exec("SELECT * FROM uploads WHERE state='pending' AND next_attempt_at <= ? "
                         "ORDER BY frozen_at, session_id LIMIT 1", (now or time.time(),)).fetchone()
        if row is None:
            return None
        return Item(row["session_id"], row["state"], json.loads(row["envelope_json"]), json.loads(row["events_json"]),
                    json.loads(row["summary_json"]), row["events_total"], row["events_sent"], row["events_resends"],
                    row["attempts"])

    def next_attempt_at(self) -> Optional[float]:
        row = self._exec("SELECT MIN(next_attempt_at) AS t FROM uploads WHERE state='pending'").fetchone()
        return row["t"]

    def counts(self) -> dict:
        out = {"open": 0, "pending": 0, "done": 0, "rejected": 0}
        for r in self._exec("SELECT state, COUNT(*) AS n FROM uploads GROUP BY state").fetchall():
            out[r["state"]] = r["n"]
        return out

    def rows(self, states: tuple = ("open", "pending", "rejected"), limit: int = 200) -> list[dict]:
        marks = ",".join("?" * len(states))
        return [dict(r) for r in self._exec(
            f"SELECT session_id, state, first_seen_at, frozen_at, events_total, events_sent, attempts, "
            f"next_attempt_at, last_code, last_error, finished_at FROM uploads WHERE state IN ({marks}) "
            f"ORDER BY first_seen_at DESC LIMIT ?", (*states, limit)).fetchall()]
