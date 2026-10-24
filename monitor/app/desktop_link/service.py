"""Background sender: live heartbeats and durable session uploads.

One daemon thread does all network I/O, one request at a time, so the GUI never
waits for the backend. The GUI hands over the latest LiveState (set_live) and
reports finished sessions (session_finished); both only take a lock.

Heartbeats (desktop_status) are sent every second while monitoring and every
10 s when idle. A heartbeat that cannot be delivered is dropped: the next one
replaces it.

Finished sessions go through the outbox: their crossing events are uploaded
in pages, then their summary. Responses are handled as follows:

  2xx                         progress / done
  401, 403, 404, 405, 3xx     configuration problem (token, network origin,
                              wrong server): all sending pauses for a minute,
                              nothing is marked failed
  429                         all sending pauses for Retry-After
  5xx, 408, network errors,   transient: retried with exponential backoff
  422 clock_skew              (2 s doubling up to 5 min), never given up
  409 events_incomplete       the events are uploaded again (twice at most)
  other 4xx                   rejected for good: kept in the outbox, not retried
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from datetime import datetime
from typing import Optional

from app.desktop_link.client import NetworkError, Response, TelemetryClient
from app.desktop_link.config import LinkConfig
from app.desktop_link.documents import (
    FINAL_STATUSES, LiveState, SessionInfo, build_session, build_status, event_pages, session_info,
)
from app.desktop_link.outbox import Item, Outbox
from app.storage.repository import Repository, iso

log = logging.getLogger("atcs_monitor.desktop_link")

HEARTBEAT_RUNNING_S = 1.0
HEARTBEAT_IDLE_S = 10.0
HEARTBEAT_RETRY_S = 5.0
UPLOAD_INTERVAL_S = 0.3  # with 1 heartbeat/s this stays below the backend's 5 requests/s
CONFIG_PAUSE_S = 60.0
RECONCILE_S = 15.0
RECORDING_GRACE_S = 120.0  # wait this long for recordings to be validated before freezing a session
MAX_BACKOFF_S = 300.0
MAX_EVENT_RESENDS = 2
TRANSIENT_CODES = ("clock_skew",)
CONFIG_STATUSES = (401, 403, 404, 405)


def backoff(attempts: int) -> float:
    return min(MAX_BACKOFF_S, 2.0 * 2 ** min(attempts, 8)) * random.uniform(0.8, 1.2)


def _epoch(ts: Optional[str]) -> Optional[float]:
    try:
        return datetime.fromisoformat(ts).timestamp() if ts else None
    except ValueError:
        return None


class LinkService(threading.Thread):
    def __init__(self, config: LinkConfig, repo: Repository, outbox: Outbox, app_version: str,
                 client: Optional[TelemetryClient] = None, clock=time.time):
        super().__init__(name="desktop-link", daemon=True)
        self.config = config
        self.repo = repo
        self.outbox = outbox
        self.app_version = app_version
        self.client = client or TelemetryClient(config)
        self.clock = clock
        # A new identity for every process (desktop_telemetry.md section 5).
        self.instance_id = uuid.uuid4().hex
        self.instance_started_at = iso(clock())
        self._seq = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop_evt = threading.Event()
        self._live = LiveState()
        self._finished: set[str] = set()  # finished by this app: observed and uploaded
        self._not_live: set[str] = set()  # found already finished in the history: never reported live
        self._observed: set[str] = set()
        self._sessions: dict[str, SessionInfo] = {}
        self._next_heartbeat = 0.0
        self._next_upload = 0.0
        self._next_reconcile = 0.0
        self._paused_until = 0.0
        self._flush_deadline: Optional[float] = None
        self._flush_heartbeat_sent = False
        self._log_times: dict[str, float] = {}
        self._next_counts = 0.0
        self._status: dict = {"connection": "starting", "detail": "", "last_ok": None, "pending": 0, "rejected": 0}

    # ------------------------------------------------------------ GUI side
    def set_live(self, live: LiveState) -> None:
        with self._lock:
            was_running = self._live.running
            self._live = live
        if live.running != was_running:
            self._next_heartbeat = 0.0
            self._wake.set()

    def session_finished(self, session_id: str) -> None:
        with self._lock:
            self._finished.add(session_id)
        self._next_reconcile = 0.0
        self._wake.set()

    def poke(self) -> None:
        """Something changed locally (a recording was finalized): check the outbox soon."""
        self._next_reconcile = 0.0
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def stop(self, flush_s: float = 3.0) -> None:
        """Stop the thread; first try for up to flush_s to deliver what is ready."""
        if self.status()["connection"] == "connected" and flush_s > 0:
            self._flush_heartbeat_sent = False
            self._flush_deadline = self.clock() + flush_s
            self._next_reconcile = self._next_heartbeat = 0.0
        else:
            self._stop_evt.set()
        self._wake.set()

    # --------------------------------------------------------------- loop
    def run(self) -> None:
        log.info("Desktop link started: backend %s, device %s, instance %s", self.config.backend_url,
                 self.config.device_name, self.instance_id)
        while not self._stop_evt.is_set():
            try:
                delay = self.run_once()
            except Exception:  # the sender must never take the app down
                log.exception("Desktop link step failed")
                delay = 5.0
            if self._flush_deadline is not None and self._flushed():
                break
            self._wake.wait(max(0.05, min(delay, 1.0)))
            self._wake.clear()
        log.info("Desktop link stopped")

    def run_once(self) -> float:
        """Do the work that is due; return seconds until something else is due."""
        now = self.clock()
        with self._lock:
            live = self._live
        if live.session_id and live.session_id not in self._observed and self._session(live) is not None:
            self.outbox.observe(live.session_id)  # running here: remember it in case the app crashes
            self._observed.add(live.session_id)
        if now >= self._next_reconcile:
            self._next_reconcile = now + RECONCILE_S
            self.reconcile(now)
        if now >= self._next_counts:
            self._next_counts = now + 2.0
            counts = self.outbox.counts()
            with self._lock:
                self._status["pending"], self._status["rejected"] = counts["pending"], counts["rejected"]
        if now < self._paused_until:
            return self._paused_until - now
        if now >= self._next_heartbeat:
            self.send_heartbeat(live, now)
            return 0.05
        if now >= self._next_upload and self.outbox.next_due(now) is not None:
            self._next_upload = now + UPLOAD_INTERVAL_S
            self.upload_step(now)
            return UPLOAD_INTERVAL_S
        due = [self._next_heartbeat, self._next_reconcile]
        nxt = self.outbox.next_attempt_at()
        if nxt is not None:
            due.append(max(nxt, self._next_upload))
        return max(0.0, min(due) - now)

    def _flushed(self) -> bool:
        """While closing: the last heartbeat went out and nothing else can be sent now."""
        now = self.clock()
        if now >= (self._flush_deadline or 0) or now < self._paused_until:
            return True
        if self.status()["connection"] != "connected":
            return True
        return self._flush_heartbeat_sent and self.outbox.next_due(now) is None

    # ---------------------------------------------------------- heartbeats
    def _session(self, live: LiveState) -> Optional[SessionInfo]:
        sid = live.session_id
        if not sid or not live.running:
            return None
        with self._lock:
            if sid in self._finished or sid in self._not_live:
                return None
        info = self._sessions.get(sid)
        if info is None:
            s = self.repo.get_session(sid)
            if s is None:
                return None
            if s["status"] != "running":
                self._not_live.add(sid)
                return None
            info = self._sessions[sid] = session_info(s)
            if len(self._sessions) > 50:
                self._sessions = {sid: info}
        return info

    def send_heartbeat(self, live: LiveState, now: float) -> None:
        self._seq += 1
        doc = build_status(live, self._session(live), instance_id=self.instance_id,
                           instance_started_at=self.instance_started_at, seq=self._seq, sent_at=iso(now),
                           app_version=self.app_version)
        interval = HEARTBEAT_RUNNING_S if live.running else HEARTBEAT_IDLE_S
        self._next_heartbeat = now + interval
        if self._flush_deadline is not None:
            self._flush_heartbeat_sent = True
        try:
            resp = self.client.post_status(doc)
        except NetworkError as exc:
            self._set_status("offline", str(exc))
            self._next_heartbeat = now + max(interval, HEARTBEAT_RETRY_S)
            return
        if resp.ok:
            was = self.status()["connection"]
            self._set_status("connected", "", last_ok=iso(now))
            if was != "connected":
                log.info("Desktop link connected to %s", self.config.backend_url)
                self.outbox.retry_now()
            if not resp.body.get("accepted", True):
                reason = resp.body.get("reason")
                if reason == "superseded":
                    self._set_status("connected", "another ATCS Monitor process is reporting as this device")
                self._log_limited(f"hb-{reason}", logging.WARNING, "Heartbeat not applied by the backend: %s", reason)
            return
        self._handle_config_or_rate(resp, now, "heartbeat")
        if resp.status not in CONFIG_STATUSES and resp.status != 429:
            if resp.code == "clock_skew":
                self._set_status("clock", "the desktop and backend clocks differ by more than 5 minutes")
            self._log_limited(f"hb-{resp.status}-{resp.code}", logging.WARNING,
                              "Heartbeat refused by the backend: HTTP %s %s", resp.status, resp.code)

    # ------------------------------------------------------------ outbox
    def reconcile(self, now: float) -> None:
        """Observe sessions reported finished; freeze open sessions that are finished locally."""
        with self._lock:
            finished = list(self._finished)
        for sid in finished:
            if sid not in self._observed:
                self.outbox.observe(sid)
                self._observed.add(sid)
        for sid in self.outbox.open_sessions():
            s = self.repo.get_session(sid)
            if s is None or s["status"] not in FINAL_STATUSES:
                continue  # still running here, or not in this app's history
            ended = _epoch(s.get("ended_at")) or now
            unfinished = [r for r in s.get("recordings") or [] if r.get("ended_at") is None]
            if unfinished and s["status"] != "interrupted" and now - ended < RECORDING_GRACE_S:
                continue  # a recording is still being validated
            try:
                frozen = build_session(s, self.repo.crossing_events(sid), iso(now))
            except (KeyError, TypeError, ValueError) as exc:
                self._log_limited(f"freeze-{sid}", logging.ERROR, "Cannot prepare session %s for upload: %s", sid, exc)
                continue
            if self.outbox.freeze(frozen, now):
                log.info("Session %s queued for upload (%s, %d crossing events, statistics %s)%s", sid,
                         frozen.summary["status"], len(frozen.events),
                         "complete" if frozen.summary["stats_complete"] else "incomplete",
                         (": " + "; ".join(frozen.notes)) if frozen.notes else "")

    def upload_step(self, now: float) -> None:
        item = self.outbox.next_due(now)
        if item is None:
            return
        sid = item.session_id
        try:
            if item.events_sent < item.events_total:
                end, page = next(iter(event_pages(item.envelope, item.events, item.events_sent)))
                resp = self.client.post_events(sid, page)
                if resp.ok:
                    self.outbox.events_progress(sid, end)
                    return
            else:
                resp = self.client.post_summary(sid, item.summary)
                if resp.ok:
                    self.outbox.done(sid, resp.body, now)
                    log.info("Session %s saved by the backend%s", sid,
                             " (already stored)" if resp.body.get("duplicate") else "")
                    return
                if resp.status == 409 and resp.code == "events_incomplete":
                    if item.events_resends < MAX_EVENT_RESENDS:
                        log.warning("Backend has %s of %d events of session %s; uploading them again",
                                    resp.body.get("stored_events"), item.events_total, sid)
                        self.outbox.resend_events(sid)
                        return
        except NetworkError as exc:
            self._set_status("offline", str(exc))
            self._transient(item, "network", str(exc), now)
            return
        self._upload_error(item, resp, now)

    def _upload_error(self, item: Item, resp: Response, now: float) -> None:
        if self._handle_config_or_rate(resp, now, "upload"):
            return
        message = str(resp.body.get("error") or f"HTTP {resp.status}")
        if resp.status >= 500 or resp.status == 408 or resp.code in TRANSIENT_CODES:
            self._transient(item, resp.code or f"http_{resp.status}", message, now)
            return
        self.outbox.reject(item.session_id, resp.code or f"http_{resp.status}", message, now)
        log.error("Backend refused session %s: HTTP %s %s - %s. It is kept in the outbox and will not be sent "
                  "again automatically (python -m app.desktop_link status).", item.session_id, resp.status,
                  resp.code, message)

    def _transient(self, item: Item, code: str, message: str, now: float) -> None:
        delay = backoff(item.attempts)
        self.outbox.failed(item.session_id, code, message, now + delay)
        self._log_limited(f"up-{code}", logging.WARNING, "Upload of session %s failed (%s); retrying in %.0f s",
                          item.session_id, code, delay)

    def _handle_config_or_rate(self, resp: Response, now: float, what: str) -> bool:
        """Pause all sending for a configuration problem or a rate limit. True if handled."""
        if resp.status == 429:
            wait = resp.retry_after or 5.0
            self._paused_until = now + wait
            self._set_status("rate_limited", f"backend asked to wait {wait:.0f} s")
            self._log_limited("429", logging.WARNING, "Backend rate limit reached; pausing %.0f s", wait)
            return True
        if resp.status in CONFIG_STATUSES or 300 <= resp.status < 400:
            state = {401: "unauthorized", 403: "refused"}.get(resp.status, "incompatible")
            detail = {
                401: "the device token was refused (revoked or wrong); set the link up again",
                403: f"the backend refused this connection ({resp.code or 'forbidden'})",
            }.get(resp.status, f"{self.config.backend_url} does not look like the ATCS backend (HTTP {resp.status})")
            self._paused_until = now + CONFIG_PAUSE_S
            self._set_status(state, detail)
            self._log_limited(f"cfg-{resp.status}", logging.ERROR, "Desktop link %s: %s", what, detail)
            return True
        return False

    # ------------------------------------------------------------ helpers
    def _set_status(self, connection: str, detail: str, last_ok: Optional[str] = None) -> None:
        with self._lock:
            self._status["connection"] = connection
            self._status["detail"] = detail
            if last_ok:
                self._status["last_ok"] = last_ok

    def _log_limited(self, key: str, level: int, msg: str, *args, every: float = 60.0) -> None:
        now = time.monotonic()
        if now - self._log_times.get(key, -every) >= every:
            self._log_times[key] = now
            log.log(level, msg, *args)
