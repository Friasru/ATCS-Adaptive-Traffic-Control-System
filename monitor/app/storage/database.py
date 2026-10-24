"""SQLite connection and schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    source_type    TEXT NOT NULL,          -- camera | video_file
    source_id      TEXT NOT NULL,          -- camera identifier or video file name
    started_at     TEXT NOT NULL,          -- ISO 8601 with offset
    ended_at       TEXT,
    status         TEXT NOT NULL,          -- running | completed | interrupted | error
    error          TEXT,
    model_name     TEXT,
    model_version  TEXT,
    device         TEXT,
    settings_json  TEXT
);
CREATE TABLE IF NOT EXISTS session_stats (
    session_id            TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
    unique_json           TEXT NOT NULL,
    crossings_json        TEXT NOT NULL,
    line_totals_json      TEXT NOT NULL,
    duration_s            REAL NOT NULL,
    frames_processed      INTEGER NOT NULL,
    avg_inference_fps     REAL,
    avg_capture_fps       REAL,
    peak_visible          INTEGER,
    avg_visible           REAL,
    recording_duration_s  REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS recordings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    codec        TEXT,
    container    TEXT,
    started_at   TEXT NOT NULL,
    ended_at     TEXT,
    frames       INTEGER,
    duration_s   REAL,
    ok           INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    info_json    TEXT
);
CREATE TABLE IF NOT EXISTS crossing_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    line_id     TEXT NOT NULL,
    track_id    INTEGER NOT NULL,
    cls         TEXT NOT NULL,
    direction   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS detection_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,             -- track_confirmed | track_lost | detection
    cls         TEXT NOT NULL,
    confidence  REAL,
    track_id    INTEGER,
    x1 REAL, y1 REAL, x2 REAL, y2 REAL
);
CREATE INDEX IF NOT EXISTS idx_cross_session ON crossing_events(session_id);
CREATE INDEX IF NOT EXISTS idx_det_session ON detection_events(session_id);
CREATE INDEX IF NOT EXISTS idx_rec_session ON recordings(session_id);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
    )
