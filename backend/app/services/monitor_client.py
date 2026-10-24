"""Client for the local ATCS Monitor service (monitor/web_service.py).

Standard library only: the backend never imports the monitor's code or its
OpenCV/YOLO dependencies, which live in a separate environment. The service
secret stays on the server; it is only ever sent to 127.0.0.1.
"""

import http.client
import json
import logging
import re
import socket

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
MIN_SECRET_BYTES = 32
MAX_JSON_BYTES = 1 << 20
MAX_FRAME_BYTES = 8 << 20
# POST /stop waits up to 10 s on the service side for the pipeline to finish.
STOP_TIMEOUT_S = 20.0
CODE_RE = re.compile(r"[a-z_]{1,40}")
# Service errors whose message is meant for the user and is passed on as is.
PASS_THROUGH = {400, 404, 409}
PASS_THROUGH_503 = {"model_unavailable", "busy", "shutting_down"}


class MonitorError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def to_response(self):
        return {"error": self.message, "code": self.code}, self.status


NOT_CONFIGURED = MonitorError(503, "service_not_configured", "monitor service is not configured")


def parse_port(value):
    text = str(value or "").strip()
    if not text.isascii() or not text.isdigit() or not 1024 <= int(text) <= 65535:
        return None
    return int(text)


class MonitorClient:
    def __init__(self, port, secret, timeout):
        self.port = port
        self._secret = secret
        self.timeout = timeout

    @classmethod
    def from_config(cls, config):
        """Fail closed: no client without a valid port and a strong secret."""
        secret = config.get("MONITOR_SERVICE_SECRET") or ""
        port = parse_port(config.get("MONITOR_SERVICE_PORT"))
        if len(secret.encode()) < MIN_SECRET_BYTES or port is None:
            log.warning("Monitor service request refused: ATCS_MONITOR_SERVICE_SECRET (32+ bytes) "
                        "and ATCS_MONITOR_PORT (1024-65535) must be set")
            raise NOT_CONFIGURED
        return cls(port, secret, float(config.get("MONITOR_SERVICE_TIMEOUT_S", 5.0)))

    def get_json(self, path):
        return self._request("GET", path)[0]

    def post_json(self, path, body=None, timeout=None):
        return self._request("POST", path, body if body is not None else {}, timeout)[0]

    def get_frame(self):
        """(jpeg bytes, frame index)"""
        body, headers = self._request("GET", "/frame.jpg", image=True)
        index = headers.get("X-Frame-Index", "")
        return body, index if index.isascii() and index.isdigit() else ""

    def _request(self, method, path, body=None, timeout=None, image=False):
        headers = {"Authorization": f"Bearer {self._secret}", "Accept": "image/jpeg" if image else "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection(HOST, self.port, timeout=timeout or self.timeout)
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            limit = MAX_FRAME_BYTES if image else MAX_JSON_BYTES
            raw = resp.read(limit + 1)
            ctype = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
            resp_headers = {"X-Frame-Index": resp.getheader("X-Frame-Index") or ""}
        except (socket.timeout, TimeoutError):
            raise MonitorError(504, "service_timeout", "monitor service did not respond in time")
        except (ConnectionError, OSError):
            raise MonitorError(503, "service_unavailable", "monitor service is not running")
        except http.client.HTTPException:
            raise MonitorError(502, "bad_service_response", "invalid response from the monitor service")
        finally:
            conn.close()
        if len(raw) > limit:
            raise MonitorError(502, "bad_service_response", "monitor service response is too large")

        if 200 <= resp.status < 300:
            if image:
                if ctype != "image/jpeg" or not raw:
                    raise MonitorError(502, "bad_service_response", "invalid response from the monitor service")
                return raw, resp_headers
            payload = self._json(raw, ctype)
            if not isinstance(payload, dict):
                raise MonitorError(502, "bad_service_response", "invalid response from the monitor service")
            return payload, resp_headers

        if resp.status in (401, 403):
            log.warning("The monitor service rejected the backend's credentials (HTTP %d); "
                        "check that both use the same ATCS_MONITOR_SERVICE_SECRET", resp.status)
            raise MonitorError(502, "service_auth_failed", "the backend could not authenticate with the monitor service")
        error = self._json(raw, ctype, strict=False)
        code = error.get("code") if isinstance(error, dict) else None
        message = error.get("error") if isinstance(error, dict) else None
        valid = isinstance(code, str) and CODE_RE.fullmatch(code) and isinstance(message, str)
        if valid and (resp.status in PASS_THROUGH or (resp.status == 503 and code in PASS_THROUGH_503)):
            raise MonitorError(resp.status, code, str(message)[:300])
        log.warning("Monitor service error on %s %s: HTTP %d %s", method, path, resp.status, code if valid else "")
        raise MonitorError(502, "service_error", "the monitor service reported an error")

    @staticmethod
    def _json(raw, ctype, strict=True):
        try:
            if ctype != "application/json":
                raise ValueError
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            if strict:
                raise MonitorError(502, "bad_service_response", "invalid response from the monitor service")
            return None
