#!/usr/bin/env python3
"""Local HTTP service that exposes the monitoring engine to the ATCS Flask backend.

One MonitoringEngine per process, listening on 127.0.0.1 only. Every request
needs ``Authorization: Bearer <ATCS_MONITOR_SERVICE_SECRET>``; the browser never
talks to this service directly, the Flask backend proxies to it.

Sources come from an allow-list built on this machine (camera indices from
ATCS_MONITOR_CAMERAS, video files directly inside ATCS_MONITOR_VIDEO_DIR); clients
pick an entry by id and can never pass a path.

Run from the monitor folder with the monitor's own environment:

    ATCS_MONITOR_SERVICE_SECRET=... .venv/bin/python web_service.py --port 8765
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Iterable, Optional
from typing import cast
from urllib.parse import urlsplit

if __package__ in (None, ""):  # `python web_service.py` from any folder
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import cv2  # noqa: E402

from app import paths  # noqa: E402
from app.camera.sources import VIDEO_EXTENSIONS, CameraSource, FrameSource, VideoFileSource  # noqa: E402
from app.detection.classes import MONITORED_CLASSES  # noqa: E402
from app.services.monitoring import MonitoringEngine, Snapshot, State  # noqa: E402
from app.settings import Settings  # noqa: E402
from app.storage.repository import Repository, iso  # noqa: E402

log = logging.getLogger("atcs_monitor.web_service")

BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SECRET_ENV = "ATCS_MONITOR_SERVICE_SECRET"
PORT_ENV = "ATCS_MONITOR_PORT"
CAMERAS_ENV = "ATCS_MONITOR_CAMERAS"
VIDEO_DIR_ENV = "ATCS_MONITOR_VIDEO_DIR"
MIN_SECRET_BYTES = 32

MAX_BODY_BYTES = 4096
MAX_CONCURRENT_REQUESTS = 16
REQUEST_TIMEOUT_S = 10.0  # socket timeout while reading a request
STOP_WAIT_S = 10.0  # how long POST /stop waits for the pipeline to finish
SHUTDOWN_WAIT_S = 30.0
JPEG_QUALITY = 80
MAX_VIDEOS = 200
MAX_CAMERA_INDEX = 63
SESSION_LIST_LIMIT = 20

# Vehicles are motor vehicles; bicycles and people are reported on their own.
VEHICLE_CLASSES = ("car", "motorcycle", "bus", "truck")
PEDESTRIAN_CLASSES = ("person",)

VIDEO_NAME_RE = re.compile(r"[A-Za-z0-9 _.()\-]{1,128}")
SOURCE_ID_RE = re.compile(r"camera:\d{1,3}|video:[A-Za-z0-9 _.()\-]{1,128}")
# Absolute POSIX, Windows drive or UNC paths; replaced by their last component.
# Quoted paths may contain spaces, so they are handled first.
QUOTED_PATH_RE = re.compile(r"(['\"])((?:[A-Za-z]:[\\/]|\\\\|/)[^'\"\n]*)\1")
ABS_PATH_RE = re.compile(r"(?<![\w.])(?:[A-Za-z]:[\\/]|\\\\|/)(?:[^\s'\"<>|:*?\\/]+[\\/])*([^\s'\"<>|:*?\\/]*)")


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def secret_is_valid(secret: Optional[str]) -> bool:
    return isinstance(secret, str) and len(secret.encode()) >= MIN_SECRET_BYTES


def redact(message: Optional[str], roots: Iterable[str] = ()) -> Optional[str]:
    """Remove local filesystem paths from a message before it leaves the process."""
    if not message:
        return message
    for root in sorted({r for r in roots if r and len(r) > 1}, key=len, reverse=True):
        message = message.replace(root, "")
    message = QUOTED_PATH_RE.sub(lambda m: m.group(1) + re.split(r"[\\/]", m.group(2))[-1] + m.group(1), message)
    return ABS_PATH_RE.sub(lambda m: m.group(1), message)[:500]


# ------------------------------------------------------------ ownership
class DataDirLocked(RuntimeError):
    pass


class DataDirLock:
    """Exclusive lock on the data folder for the lifetime of one service process.

    The operating system releases it when the process ends, even after a crash or
    kill, so while it is held no other monitor service is using this database.
    The desktop app does not take it.
    """

    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise DataDirLocked("another monitor service is already using this data folder")
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()  # closing the handle releases the lock
            self._fh = None


class ServiceRepository(Repository):
    """Repository that remembers which sessions this service created.

    The sessions table has no owner column (and the imported schema is not
    changed), so the service keeps its own journal table. The id is journaled
    before the session row exists, so every session the service creates is in it.
    """

    def __init__(self, path):
        super().__init__(path)
        with self.transaction() as c:
            c.execute("CREATE TABLE IF NOT EXISTS web_service_sessions "
                      "(session_id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")

    def create_session(self, session_id: str, *args, **kwargs) -> None:
        with self.transaction() as c:
            c.execute("INSERT OR IGNORE INTO web_service_sessions(session_id, created_at) VALUES (?, ?)",
                      (session_id, iso()))
        super().create_session(session_id, *args, **kwargs)

    def recover_interrupted_sessions(self) -> int:
        """Mark this service's sessions that a crashed process left 'running' as 'interrupted'.

        Call only while holding the DataDirLock: then no live service owns any
        journaled session. Sessions started by the desktop app are never in the
        journal and are left alone.
        """
        owned = "SELECT session_id FROM web_service_sessions"
        with self.transaction() as c:
            cur = c.execute(
                "UPDATE sessions SET status='interrupted', ended_at=COALESCE(ended_at, ?), "
                f"error=COALESCE(error, 'Monitor service ended unexpectedly') WHERE status='running' AND session_id IN ({owned})",
                (iso(),),
            )
            c.execute(
                "UPDATE recordings SET error=COALESCE(error, 'Recording not finalized (monitor service ended unexpectedly)') "
                f"WHERE ended_at IS NULL AND session_id IN ({owned})"
            )
            c.execute("DELETE FROM web_service_sessions")  # all of them are finished now
            return cur.rowcount


# ------------------------------------------------------------------ sources
@dataclass(frozen=True)
class SourceEntry:
    id: str
    kind: str  # camera | video_file
    label: str
    camera_index: Optional[int] = None
    path: Optional[Path] = None

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label}


class SourceCatalog:
    """Allow-listed sources. Client input is only ever used as a dictionary key."""

    def __init__(self, camera_indices: Iterable[int] = (), video_dir: Optional[Path] = None,
                 camera_size: tuple[int, int] = (640, 480)):
        self.camera_indices = sorted({int(i) for i in camera_indices})
        for i in self.camera_indices:
            if not 0 <= i <= MAX_CAMERA_INDEX:
                raise ValueError(f"camera index {i} is outside 0-{MAX_CAMERA_INDEX}")
        self.video_dir = Path(video_dir).resolve() if video_dir else None
        self.camera_size = camera_size

    def entries(self) -> dict[str, SourceEntry]:
        out: dict[str, SourceEntry] = {}
        for i in self.camera_indices:
            out[f"camera:{i}"] = SourceEntry(f"camera:{i}", "camera", f"Camera {i}", camera_index=i)
        for p in self._videos():
            out[f"video:{p.name}"] = SourceEntry(f"video:{p.name}", "video_file", p.name, path=p)
        return out

    def _videos(self) -> list[Path]:
        if self.video_dir is None or not self.video_dir.is_dir():
            return []
        found = []
        try:
            candidates = sorted(self.video_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            log.warning("Cannot list the video folder: %s", exc)
            return []
        for p in candidates:
            if p.suffix.lower() not in VIDEO_EXTENSIONS or not VIDEO_NAME_RE.fullmatch(p.name):
                continue
            try:
                # Symlinks that lead out of the folder are not part of the allow-list.
                if not p.is_file() or p.resolve().parent != self.video_dir:
                    continue
            except OSError:
                continue
            found.append(p)
            if len(found) >= MAX_VIDEOS:
                break
        return found

    def get(self, source_id: object) -> SourceEntry:
        if not isinstance(source_id, str) or not SOURCE_ID_RE.fullmatch(source_id):
            raise ServiceError(400, "invalid_source", "source must be the id of an allowed source")
        entry = self.entries().get(source_id)
        if entry is None:
            raise ServiceError(400, "unknown_source", "source is not in the allowed source list")
        return entry

    def build_source(self, entry: SourceEntry) -> FrameSource:
        if entry.kind == "camera":
            assert entry.camera_index is not None
            w, h = self.camera_size
            return CameraSource(entry.camera_index, w, h, name=entry.label)
        assert entry.path is not None
        return VideoFileSource(entry.path, realtime=True)


# ------------------------------------------------------------------ service
class MonitorService:
    """Thread-safe facade over one MonitoringEngine."""

    def __init__(self, settings: Settings, repo: Repository, catalog: SourceCatalog, detector=None,
                 recordings_dir: Optional[Path] = None, stop_wait_s: float = STOP_WAIT_S):
        self.settings = settings.copy()
        self.repo = repo
        self.catalog = catalog
        self.stop_wait_s = stop_wait_s
        self._control = threading.Lock()  # start/stop/recording transitions
        self._info = threading.Lock()  # fields written by engine callbacks
        self._message = ""
        self._last_error: Optional[str] = None
        self._current: Optional[SourceEntry] = None
        self._stale: Optional[Snapshot] = None  # snapshot left over from before the current start
        self._closing = False
        self._jpeg_lock = threading.Lock()
        self._jpeg_snap: Optional[Snapshot] = None
        self._jpeg = b""
        self.jpeg_encodes = 0
        self.engine = MonitoringEngine(self.settings, repo, on_state=self._on_state, on_error=self._on_error,
                                       detector=detector, recordings_dir=recordings_dir)

    # engine callbacks run on worker threads; they must never take self._control
    def _on_state(self, state: State, message: str) -> None:
        with self._info:
            self._message = self._redact(message) or ""

    def _on_error(self, message: str) -> None:
        with self._info:
            self._last_error = self._redact(message)

    def _redact(self, message: Optional[str]) -> Optional[str]:
        roots = [str(paths.project_root()), str(Path.home())]
        if self.catalog.video_dir is not None:
            roots.append(str(self.catalog.video_dir))
        return redact(message, roots)

    # ------------------------------------------------------------- queries
    def sources(self) -> dict:
        return {"sources": [e.public() for e in self.catalog.entries().values()]}

    def _model_available(self) -> bool:
        d = self.engine.detector
        if d is not None and getattr(d, "model", None) is not None:
            return True
        return paths.find_model(self.settings.model_name).is_file()

    def _fresh_snapshot(self) -> Optional[Snapshot]:
        snap = self.engine.latest_snapshot()
        return None if snap is self._stale or not snap.session_id else snap

    def status(self) -> dict:
        state = self.engine.state
        running = self.engine.is_running
        snap = self._fresh_snapshot()
        with self._info:
            message, last_error, current = self._message, self._last_error, self._current

        def counts(d: Optional[dict]) -> dict:
            return {c: int((d or {}).get(c, 0)) for c in MONITORED_CLASSES}

        visible = counts(snap.visible if snap else None)
        unique = counts(snap.unique if snap else None)
        crossings = counts(snap.crossings if snap else None)

        def total(d: dict, classes: tuple[str, ...]) -> int:
            return sum(d[c] for c in classes)

        report = self.engine.last_report
        return {
            "running": running,
            "state": state.value,
            "message": message,
            "source": current.public() if current else None,
            "session_id": snap.session_id if snap else None,
            "recording": {
                "active": state == State.RECORDING,
                "file": Path(snap.recording_path).name if snap and snap.recording_path else None,
            },
            "classes": list(MONITORED_CLASSES),
            "vehicle_classes": list(VEHICLE_CLASSES),
            "visible": visible,
            "unique": unique,
            "crossings": crossings,
            "line_totals": _line_totals(snap.line_totals) if snap else {},
            "totals": {
                "visible_vehicles": total(visible, VEHICLE_CLASSES),
                "unique_vehicles": total(unique, VEHICLE_CLASSES),
                "crossing_vehicles": total(crossings, VEHICLE_CLASSES),
                "visible_pedestrians": total(visible, PEDESTRIAN_CLASSES),
                "unique_pedestrians": total(unique, PEDESTRIAN_CLASSES),
                "crossing_pedestrians": total(crossings, PEDESTRIAN_CLASSES),
            },
            "performance": {
                "capture_fps": round(snap.capture_fps, 2) if snap else 0.0,
                "inference_fps": round(snap.inference_fps, 2) if snap else 0.0,
                "recording_fps": round(snap.recording_fps, 2) if snap else 0.0,
                "latency_ms": round(snap.latency_ms, 1) if snap else 0.0,
                "frames_processed": snap.frames_processed if snap else 0,
                "frames_dropped": snap.frames_dropped if snap else 0,
                "recording_frames_dropped": snap.recording_frames_dropped if snap else 0,
                "active_tracks": snap.active_tracks if snap else 0,
            },
            "device": snap.device if snap else None,
            "model": {"name": self.settings.model_name, "available": self._model_available()},
            "last_error": last_error,
            "last_session": self._report(report) if report else None,
        }

    def _report(self, report) -> dict:
        stats = report.stats or {}
        return {
            "session_id": report.session_id,
            "status": report.status,
            "error": self._redact(report.error),
            "duration_s": stats.get("duration_s"),
            "frames_processed": stats.get("frames_processed"),
            "unique": {c: int((stats.get("unique") or {}).get(c, 0)) for c in MONITORED_CLASSES},
            "crossings": {c: int((stats.get("crossings") or {}).get(c, 0)) for c in MONITORED_CLASSES},
        }

    def sessions(self) -> dict:
        rows = self.repo.list_sessions(limit=SESSION_LIST_LIMIT)
        out = []
        for r in rows:
            out.append({
                "session_id": r["session_id"],
                "source_type": r["source_type"],
                "source_id": self._redact(r["source_id"]),
                "status": r["status"],
                "started_at": r["started_at"],
                "ended_at": r["ended_at"],
                "error": self._redact(r["error"]),
                "model_name": r["model_name"],
                "device": r["device"],
                "duration_s": r["duration_s"],
                "frames_processed": r["frames_processed"],
                "unique": r["unique"],
                "crossings": r["crossings"],
                "recordings": len(r["recording_paths"]),
            })
        return {"sessions": out}

    def frame_jpeg(self) -> tuple[bytes, int]:
        """Latest annotated frame as JPEG. Encoded once per new frame, shared by all viewers."""
        snap = self._fresh_snapshot()
        if snap is None or snap.frame is None:
            raise ServiceError(404, "no_frame", "no frame is available yet")
        with self._jpeg_lock:
            if snap is not self._jpeg_snap:
                ok, buf = cv2.imencode(".jpg", snap.frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
                if not ok:
                    raise ServiceError(500, "encode_failed", "frame could not be encoded")
                self._jpeg_snap, self._jpeg = snap, buf.tobytes()
                self.jpeg_encodes += 1
            return self._jpeg, snap.frame_index

    # ------------------------------------------------------------- commands
    def start(self, source_id: object, loop: bool = False) -> dict:
        entry = self.catalog.get(source_id)
        with self._control:
            if self._closing:
                raise ServiceError(503, "shutting_down", "the monitor service is shutting down")
            if self.engine.is_running:
                raise ServiceError(409, "already_running", "monitoring is already running; stop it first")
            if not self._model_available():
                raise ServiceError(503, "model_unavailable",
                                   f"model weights '{self.settings.model_name}' are not installed")
            source = self.catalog.build_source(entry)
            with self._info:
                self._last_error = None
                self._message = ""
                self._current = entry
            self._stale = self.engine.latest_snapshot()
            if not self.engine.start(source, loop=loop and entry.kind == "video_file"):
                raise ServiceError(409, "already_running", "monitoring is already running; stop it first")
        log.info("Monitoring started on %s", entry.id)
        return self.status()

    def stop(self) -> dict:
        with self._control:
            if not self.engine.is_running:
                raise ServiceError(409, "not_running", "monitoring is not running")
            self.engine.stop()
            deadline = time.monotonic() + self.stop_wait_s
            while self.engine.is_running and time.monotonic() < deadline:
                time.sleep(0.05)
        log.info("Monitoring stop requested")
        return self.status()

    def start_recording(self) -> dict:
        with self._control:
            state = self.engine.state
            if not self.engine.is_running or state in (State.STOPPING, State.ERROR, State.IDLE):
                raise ServiceError(409, "not_running", "monitoring is not running")
            if state == State.INITIALIZING:
                raise ServiceError(409, "not_ready", "monitoring is still starting; try again shortly")
            if state == State.RECORDING:
                raise ServiceError(409, "already_recording", "recording is already active")
            self.engine.start_recording()
        return {**self.status(), "recording_requested": True}

    def stop_recording(self) -> dict:
        with self._control:
            if self.engine.state != State.RECORDING:
                raise ServiceError(409, "not_recording", "recording is not active")
            self.engine.stop_recording()
        return {**self.status(), "recording_stop_requested": True}

    def close(self, timeout: float = SHUTDOWN_WAIT_S) -> None:
        """Stop monitoring and wait for the camera, recorder and database writer to finish."""
        with self._control:
            self._closing = True
            if self.engine.is_running:
                log.info("Stopping monitoring for shutdown")
            self.engine.stop(wait=True, timeout=timeout)


def _line_totals(raw: dict) -> dict:
    return {str(line): {str(d): {c: int(n) for c, n in counts.items()} for d, counts in dirs.items()}
            for line, dirs in (raw or {}).items()}


# --------------------------------------------------------------------- HTTP
def _no_body(body: dict) -> None:
    if body:
        raise ServiceError(400, "invalid_body", "this endpoint takes no parameters")


def _start(service: MonitorService, body: dict):
    unknown = set(body) - {"source", "loop"}
    if unknown:
        raise ServiceError(400, "invalid_body", "unknown field(s): " + ", ".join(sorted(map(str, unknown)))[:200])
    loop = body.get("loop", False)
    if not isinstance(loop, bool):
        raise ServiceError(400, "invalid_body", "loop must be true or false")
    return 202, service.start(body.get("source"), loop)


def _stop(service: MonitorService, body: dict):
    _no_body(body)
    return 200, service.stop()


def _rec_start(service: MonitorService, body: dict):
    _no_body(body)
    return 202, service.start_recording()


def _rec_stop(service: MonitorService, body: dict):
    _no_body(body)
    return 202, service.stop_recording()


Route = Callable[[MonitorService, dict], tuple]

ROUTES: dict[str, dict[str, Route]] = {
    "/status": {"GET": lambda s, b: (200, s.status())},
    "/sources": {"GET": lambda s, b: (200, s.sources())},
    "/sessions": {"GET": lambda s, b: (200, s.sessions())},
    "/frame.jpg": {"GET": lambda s, b: (200, s.frame_jpeg())},
    "/start": {"POST": _start},
    "/stop": {"POST": _stop},
    "/recording/start": {"POST": _rec_start},
    "/recording/stop": {"POST": _rec_stop},
}


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Linux/macOS: SO_REUSEADDR only allows a quick restart while old connections are in
    # TIME_WAIT; it never lets two servers listen at once. On Windows it would, so the port
    # is claimed with SO_EXCLUSIVEADDRUSE instead.
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, port: int, service: MonitorService, secret: str,
                 max_concurrent: int = MAX_CONCURRENT_REQUESTS):
        if not secret_is_valid(secret):
            raise ValueError(f"the service secret must be at least {MIN_SECRET_BYTES} bytes")
        self.service = service
        self._secret_digest = hashlib.sha256(secret.encode()).digest()
        self.slots = threading.BoundedSemaphore(max_concurrent)
        super().__init__((BIND_HOST, port), Handler)

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # HTTPServer.server_bind would also resolve the host name, which can stall.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = BIND_HOST, int(self.server_address[1])

    def token_ok(self, header: Optional[str]) -> bool:
        scheme, _, token = (header or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return False
        return hmac.compare_digest(hashlib.sha256(token.strip().encode()).digest(), self._secret_digest)


class Handler(BaseHTTPRequestHandler):
    server_version = "ATCSMonitorService"
    sys_version = ""
    timeout = REQUEST_TIMEOUT_S

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    def do_HEAD(self):
        self._dispatch("HEAD")

    def log_message(self, format, *args):  # request line only; headers (and the token) are never logged
        log.debug("%s %s", self.address_string(), format % args)

    def _dispatch(self, method: str) -> None:
        srv = cast(MonitorHTTPServer, self.server)
        if not srv.slots.acquire(blocking=False):
            self._send_error(503, "busy", "too many concurrent requests")
            return
        try:
            if not srv.token_ok(self.headers.get("Authorization")):
                self._send_error(401, "unauthorized", "missing or invalid service token")
                return
            path = urlsplit(self.path).path
            methods = ROUTES.get(path)
            if methods is None:
                self._send_error(404, "not_found", "unknown endpoint")
                return
            route = methods.get(method)
            if route is None:
                self._send_error(405, "method_not_allowed", "method not allowed", {"Allow": ", ".join(methods)})
                return
            body = self._read_json() if method == "POST" else {}
            status, payload = route(srv.service, body)
            if path == "/frame.jpg":
                jpeg, index = payload
                self._send(status, jpeg, "image/jpeg", {"X-Frame-Index": str(index)})
            else:
                self._send_json(status, payload)
        except ServiceError as exc:
            self._send_error(exc.status, exc.code, exc.message)
        except Exception:
            log.exception("Request %s %s failed", method, urlsplit(self.path).path)
            self._send_error(500, "internal_error", "internal monitor service error")
        finally:
            srv.slots.release()

    def _read_json(self) -> dict:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or raw_len.strip() in ("", "0"):
            return {}
        try:
            length = int(raw_len)
        except ValueError:
            raise ServiceError(400, "invalid_body", "invalid Content-Length")
        if length < 0:
            raise ServiceError(400, "invalid_body", "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            self.close_connection = True
            raise ServiceError(413, "body_too_large", f"request body is larger than {MAX_BODY_BYTES} bytes")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ServiceError(415, "unsupported_media_type", "request body must be application/json")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ServiceError(400, "invalid_body", "request body is not valid JSON")
        except OSError:
            raise ServiceError(400, "invalid_body", "request body could not be read")
        if not isinstance(data, dict):
            raise ServiceError(400, "invalid_body", "request body must be a JSON object")
        return data

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict, extra: Optional[dict] = None) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json", extra)

    def _send_error(self, status: int, code: str, message: str, extra: Optional[dict] = None) -> None:
        try:
            self._send_json(status, {"error": message, "code": code}, extra)
        except OSError:
            pass  # client went away


# --------------------------------------------------------------------- main
def parse_cameras(text: str) -> list[int]:
    out = []
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        if not part.isdigit() or int(part) > MAX_CAMERA_INDEX:
            raise ValueError(f"{CAMERAS_ENV} must list camera indices 0-{MAX_CAMERA_INDEX} (got {part!r})")
        out.append(int(part))
    return out


def parse_port(text: str) -> int:
    if not text.isdigit() or not 1024 <= int(text) <= 65535:
        raise ValueError(f"port must be a number between 1024 and 65535 (got {text!r})")
    return int(text)


def setup_logging(verbose: bool) -> None:
    handler = logging.handlers.RotatingFileHandler(paths.logs_dir() / "monitor_service.log", maxBytes=2_000_000,
                                                   backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)
    logging.getLogger("ultralytics").setLevel(logging.ERROR)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="ATCS Monitor local service for the ATCS web backend (127.0.0.1 only)")
    ap.add_argument("--port", default=os.environ.get(PORT_ENV, str(DEFAULT_PORT)),
                    help=f"TCP port on 127.0.0.1 (default ${PORT_ENV} or {DEFAULT_PORT})")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    secret = os.environ.get(SECRET_ENV, "")
    if not secret_is_valid(secret):
        print(f"error: {SECRET_ENV} must be set to a random value of at least {MIN_SECRET_BYTES} bytes, e.g.\n"
              "  python3 -c 'import secrets; print(secrets.token_hex(32))'", file=sys.stderr)
        return 2
    try:
        port = parse_port(str(args.port))
        cameras = parse_cameras(os.environ.get(CAMERAS_ENV, ""))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    paths.configure_third_party_dirs()  # before ultralytics is imported
    setup_logging(args.verbose)

    from app.settings import load_settings

    settings = load_settings()
    video_dir = Path(os.environ.get(VIDEO_DIR_ENV) or paths.data_dir() / "test_media")
    catalog = SourceCatalog(cameras, video_dir, (settings.camera_width, settings.camera_height))
    lock = DataDirLock(paths.data_dir() / "monitor_service.lock")
    try:
        lock.acquire()
    except DataDirLocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    repo = ServiceRepository(paths.database_path())
    recovered = repo.recover_interrupted_sessions()
    if recovered:
        log.warning("%d session(s) left running by an earlier monitor service are now marked 'interrupted'", recovered)
    service = MonitorService(settings, repo, catalog)
    try:
        server = MonitorHTTPServer(port, service, secret)
    except OSError as exc:
        print(f"error: cannot listen on {BIND_HOST}:{port} ({exc.strerror or exc}); "
              "is another monitor service running?", file=sys.stderr)
        repo.close()
        lock.release()
        return 2

    def request_shutdown(signum, frame):
        log.info("Signal %s received; shutting down", signum)
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request_shutdown)

    entries = catalog.entries()
    log.info("Monitor service listening on http://%s:%d (%d camera(s), %d video(s) allowed)", BIND_HOST,
             server.server_port, sum(e.kind == "camera" for e in entries.values()),
             sum(e.kind == "video_file" for e in entries.values()))
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        service.close()
        repo.close()
        lock.release()
        log.info("Monitor service stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
