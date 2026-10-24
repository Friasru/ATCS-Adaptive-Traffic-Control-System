"""HTTP client for the backend's /api/desktop/telemetry/* endpoints.

Standard library only. Requests go straight to the configured loopback address:
system proxies are ignored (a proxy would also make the backend refuse the
request) and redirects are never followed, so the token cannot be forwarded
anywhere else. No browser headers (Origin, Cookie, Sec-Fetch-*) are sent.

The token is only ever placed in the Authorization header. It is not part of
any exception, log line or Response.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Optional

from app.desktop_link.config import LinkConfig

USER_AGENT = "ATCS-Monitor-Desktop-Link/1.0"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_RETRY_AFTER_S = 300.0


class NetworkError(Exception):
    """The backend could not be reached (refused, timeout, DNS, reset)."""


@dataclass
class Response:
    status: int
    body: dict = field(default_factory=dict)
    retry_after: Optional[float] = None

    @property
    def code(self) -> str:
        return str(self.body.get("code") or "")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None  # a 3xx is returned to the caller as an error response


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
    if seconds != seconds:
        return None
    return max(1.0, min(MAX_RETRY_AFTER_S, seconds))


class TelemetryClient:
    def __init__(self, config: LinkConfig, timeout: float = 5.0):
        self._base = config.backend_url.rstrip("/")
        self._token = config.token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def post_status(self, doc: dict) -> Response:
        return self._post("/api/desktop/telemetry/status", doc)

    def post_events(self, session_id: str, doc: dict) -> Response:
        return self._post(f"/api/desktop/telemetry/sessions/{session_id}/events", doc)

    def post_summary(self, session_id: str, doc: dict) -> Response:
        return self._post(f"/api/desktop/telemetry/sessions/{session_id}/summary", doc)

    def probe(self) -> Response:
        """Check address, network origin and token without storing anything.

        The backend checks origin (403) and token (401) before the content type,
        so a deliberately non-JSON request answers 415 when both are accepted.
        """
        return self._post("/api/desktop/telemetry/status", None, content_type="text/plain")

    def _post(self, path: str, doc: Optional[dict], content_type: str = "application/json") -> Response:
        if doc is None:
            data = b"probe"
        else:
            data = json.dumps(doc, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(self._base + path, data=data, method="POST", headers={
            "Content-Type": content_type,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": "Bearer " + self._token.reveal(),
        })
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return Response(resp.status, _json(resp.read(MAX_RESPONSE_BYTES)))
        except urllib.error.HTTPError as exc:
            try:
                body = _json(exc.read(MAX_RESPONSE_BYTES))
            except OSError:
                body = {}
            finally:
                exc.close()
            return Response(exc.code, body, parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None))
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"backend unreachable at {self._base}: {type(reason).__name__}") from None


def _json(raw: bytes) -> dict:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}
