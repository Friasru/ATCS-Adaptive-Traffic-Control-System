"""/api/desktop/telemetry: write-only endpoints for desktop ATCS Monitor apps.

Authenticated with a device token (Authorization: Bearer atcsdev_...) issued by
backend/monitor_access.py. Browser JWTs are not accepted here, and device tokens
are not accepted by any other endpoint (they are not JWTs).

Order of checks: network origin (403), device token (401), per-device rate
limit (429), content type (415), length (411/413), document (400), then the
semantic checks (409/422). No CORS headers are ever added; requests that carry
browser or proxy headers are refused.

Failed credentials are not locked out by client address: every accepted client
is on this computer, so such a lockout would let any local program block the
real desktop. Tokens are 256-bit random values, so guessing is not a practical
attack; only the warnings about failed credentials are rate-limited, to keep
the log readable.
"""

import ipaddress
import logging
import threading
import time
from functools import wraps

from flask import current_app, request

from app import db
from app.api import api_bp
from app.services import desktop_schema, monitor_devices
from app.services.desktop_telemetry import TelemetryError, ingest_events, ingest_status, ingest_summary

log = logging.getLogger(__name__)

STATUS_MAX_BYTES = 8 * 1024
EVENTS_MAX_BYTES = 128 * 1024
SUMMARY_MAX_BYTES = 32 * 1024
# A reverse proxy on this machine would make remote clients look local.
PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP")
# Sent by browsers; the desktop client never sends them.
BROWSER_HEADERS = ("Origin", "Cookie", "Sec-Fetch-Site", "Sec-Fetch-Mode")
UNAUTHORIZED = {"error": "device authentication required", "code": "device_unauthorized"}


class TokenBucket:
    """In-memory rate limiter: `rate` tokens per second, at most `burst` saved up."""

    MAX_KEYS = 10_000

    def __init__(self, rate, burst, clock=time.monotonic):
        self.rate = float(rate)
        self.burst = float(burst)
        self.clock = clock
        self._buckets = {}
        self._lock = threading.Lock()

    def _level(self, key, now):
        tokens, last = self._buckets.get(key, (self.burst, now))
        return min(self.burst, tokens + (now - last) * self.rate)

    def take(self, key):
        """(allowed, seconds until the next token)."""
        with self._lock:
            now = self.clock()
            tokens = self._level(key, now)
            if len(self._buckets) >= self.MAX_KEYS and key not in self._buckets:
                self._buckets.clear()
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False, (1.0 - tokens) / self.rate if self.rate > 0 else 60.0
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0


def limiters():
    ext = current_app.extensions
    if "atcs_desktop_limits" not in ext:
        cfg = current_app.config
        ext["atcs_desktop_limits"] = {
            "device": TokenBucket(cfg["DESKTOP_RATE_PER_S"], cfg["DESKTOP_RATE_BURST"]),
            "auth_log": TokenBucket(cfg["DESKTOP_AUTH_FAILURE_LOGS_PER_MIN"] / 60.0,
                                    cfg["DESKTOP_AUTH_FAILURE_LOGS_PER_MIN"]),
        }
    return ext["atcs_desktop_limits"]


def is_loopback(addr):
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def too_many(retry_after):
    return ({"error": "too many requests", "code": "rate_limited"}, 429,
            {"Retry-After": str(max(1, int(retry_after + 0.999)))})


def bearer_token():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return token if scheme == "Bearer" and token and " " not in token else None


def device_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        addr = request.remote_addr
        limits = limiters()
        if not is_loopback(addr) or any(h in request.headers for h in PROXY_HEADERS):
            if limits["auth_log"].take("origin")[0]:
                log.warning("Desktop telemetry refused from non-loopback or proxied client %s", addr)
            return {"error": "desktop telemetry is accepted from this computer only", "code": "origin_refused"}, 403
        if any(h in request.headers for h in BROWSER_HEADERS):
            return {"error": "desktop telemetry does not accept browser requests", "code": "browser_refused"}, 403
        device = monitor_devices.authenticate(bearer_token())
        if device is None:
            if limits["auth_log"].take("failures")[0]:
                log.warning("Desktop telemetry: rejected a missing or invalid device credential")
            return UNAUTHORIZED, 401, {"WWW-Authenticate": 'Bearer realm="atcs-desktop"'}
        allowed, retry = limits["device"].take(device.id)
        if not allowed:
            return too_many(retry)
        try:
            return fn(device, *args, **kwargs)
        except TelemetryError as exc:
            db.session.rollback()
            if exc.status >= 409:
                log.warning("Desktop telemetry from %s refused: %s", device.name, exc.code)
            return exc.to_response()

    return wrapper


def read_json(limit):
    if request.mimetype != "application/json":
        raise TelemetryError(415, "unsupported_media_type", "Content-Type must be application/json")
    length = request.content_length
    if length is None:
        raise TelemetryError(411, "length_required", "Content-Length is required")
    if length > limit:
        raise TelemetryError(413, "payload_too_large", f"body is larger than {limit} bytes")
    raw = request.get_data(cache=False)
    if len(raw) > limit:
        raise TelemetryError(413, "payload_too_large", f"body is larger than {limit} bytes")
    try:
        return desktop_schema.loads(raw)
    except desktop_schema.SchemaError as exc:
        raise TelemetryError(400, "invalid_json", str(exc))


def session_id_from_path(session_id):
    if not desktop_schema.HEX32_RE.fullmatch(session_id):
        raise TelemetryError(400, "invalid_session_id", "session id must be 32 lowercase hex characters")
    return session_id


@api_bp.route("/desktop/telemetry/status", methods=["POST"])
@device_required
def desktop_status_post(device):
    return ingest_status(device, read_json(STATUS_MAX_BYTES), current_app.config)


@api_bp.route("/desktop/telemetry/sessions/<session_id>/events", methods=["POST"])
@device_required
def desktop_events_post(device, session_id):
    session_id = session_id_from_path(session_id)
    return ingest_events(device, session_id, read_json(EVENTS_MAX_BYTES), current_app.config)


@api_bp.route("/desktop/telemetry/sessions/<session_id>/summary", methods=["POST"])
@device_required
def desktop_summary_post(device, session_id):
    session_id = session_id_from_path(session_id)
    body, created = ingest_summary(device, session_id, read_json(SUMMARY_MAX_BYTES), current_app.config)
    return body, 201 if created else 200
