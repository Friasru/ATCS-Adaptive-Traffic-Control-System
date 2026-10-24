"""Desktop link configuration, token storage, HTTP client hygiene and the admin CLI."""

import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.desktop_link import __main__ as cli
from app.desktop_link import config
from app.desktop_link.client import NetworkError, TelemetryClient
from tests.link_support import TOKEN, link_config

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permissions")


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:5000", "http://127.0.0.1:5000"),
    ("http://127.0.0.1:5000/", "http://127.0.0.1:5000"),
    ("http://localhost:5000", "http://localhost:5000"),
    ("https://LOCALHOST:8443", "https://localhost:8443"),
    ("http://[::1]:5000", "http://[::1]:5000"),
    ("http://127.0.0.1", "http://127.0.0.1"),
])
def test_backend_url_accepted(url, expected):
    assert config.validate_backend_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://192.168.1.10:5000", "http://example.com", "http://127.0.0.1.evil.com:5000", "ftp://127.0.0.1",
    "http://user:pw@127.0.0.1:5000", "http://127.0.0.1:5000/api", "http://127.0.0.1:5000?x=1",
    "http://127.0.0.1:5000#x", "127.0.0.1:5000", "http://127.0.0.1:99999", "http://0.0.0.0:5000",
    "http://172.17.0.1:5000", "", None, 5000,
])
def test_backend_url_refused(url):
    with pytest.raises(config.ConfigError):
        config.validate_backend_url(url)


@pytest.mark.parametrize("name", ["ab", "a" * 65, "bad name", "x/y", "", None])
def test_device_name_refused(name):
    with pytest.raises(config.ConfigError):
        config.validate_device_name(name)


@pytest.mark.parametrize("token", ["", "atcsdev_short", "eyJhbGciOiJIUzI1NiJ9.e30.x", "Bearer " + TOKEN,
                                   TOKEN + "x", TOKEN[:-1] + "!", None])
def test_token_format_refused(token):
    with pytest.raises(config.ConfigError) as exc:
        config.validate_token(token)
    if token:
        assert token not in str(exc.value)


def test_secret_never_shows_its_value():
    s = config.Secret(TOKEN)
    cfg = link_config()
    for text in (str(s), repr(s), f"{s}", repr(cfg), str(cfg)):
        assert TOKEN not in text
    assert s.reveal() == TOKEN


@posix_only
def test_token_roundtrip_with_private_permissions(home):
    path = config.save_token(config.Secret(TOKEN))
    assert path.parent == config.link_dir() and str(home) in str(path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert config.load_token() == config.Secret(TOKEN)


@posix_only
def test_token_readable_by_others_is_refused(home):
    path = config.save_token(config.Secret(TOKEN))
    os.chmod(path, 0o644)
    with pytest.raises(config.ConfigError) as exc:
        config.load_token()
    assert "chmod 600" in str(exc.value) and TOKEN not in str(exc.value)


@posix_only
@pytest.mark.parametrize("content", ["garbage", "plain:atcsdev_x", "dpapi:AAAA", ""])
def test_damaged_token_file(home, content):
    path = config.token_path()
    config._write_private(path, content)
    with pytest.raises(config.ConfigError):
        config.load_token()


def test_missing_config_means_standalone(home):
    assert config.read_config_file() is None
    assert config.load_config() is None


@posix_only
def test_config_roundtrip_and_disable(home):
    config.save_token(config.Secret(TOKEN))
    config.save_config("http://127.0.0.1:5000/", "my-desk")
    cfg = config.load_config()
    assert cfg.backend_url == "http://127.0.0.1:5000" and cfg.device_name == "my-desk"
    assert cfg.token.reveal() == TOKEN
    assert TOKEN not in config.config_path().read_text()  # the token is never in the JSON file
    config.save_config("http://127.0.0.1:5000", "my-desk", enabled=False)
    assert config.load_config() is None


def test_config_with_token_missing_is_an_error(home):
    config.save_config("http://127.0.0.1:5000", "my-desk")
    with pytest.raises(config.ConfigError):
        config.load_config()


@pytest.mark.parametrize("doc", [{"backend_url": "http://10.0.0.1:5000", "device_name": "abc"},
                                 {"backend_url": "http://127.0.0.1:5000", "device_name": "abc", "token": "x"},
                                 {"backend_url": "http://127.0.0.1:5000", "device_name": "abc", "enabled": "yes"},
                                 ["not", "an", "object"]])
def test_bad_config_file(home, doc):
    config.config_path().write_text(json.dumps(doc))
    with pytest.raises(config.ConfigError):
        config.read_config_file()


def test_link_files_live_in_the_data_folder(home):
    assert config.link_dir() == home / "data" / "desktop_link"
    assert config.outbox_path().parent == config.link_dir()


# ------------------------------------------------------------- HTTP client
class Stub(BaseHTTPRequestHandler):
    seen: list = []
    answer = (415, {"error": "x", "code": "unsupported_media_type"}, {})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        Stub.seen.append((self.path, dict(self.headers), self.rfile.read(length)))
        status, body, headers = Stub.answer
        data = json.dumps(body).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    Stub.seen = []
    Stub.answer = (415, {"error": "x", "code": "unsupported_media_type"}, {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def test_client_headers_and_body(stub, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # would break the request if it were used
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    Stub.answer = (200, {"accepted": True}, {})
    client = TelemetryClient(link_config(stub))
    resp = client.post_status({"kind": "desktop_status", "n": 1})
    assert resp.status == 200 and resp.body == {"accepted": True}
    path, headers, body = Stub.seen[0]
    assert path == "/api/desktop/telemetry/status"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    for h in ("Origin", "Cookie", "Sec-Fetch-Site", "Sec-Fetch-Mode", "X-Forwarded-For", "Forwarded"):
        assert h not in headers
    assert json.loads(body) == {"kind": "desktop_status", "n": 1}


def test_client_paths(stub):
    Stub.answer = (200, {}, {})
    client = TelemetryClient(link_config(stub))
    sid = "a" * 32
    client.post_events(sid, {})
    client.post_summary(sid, {})
    assert [p for p, _, _ in Stub.seen] == [f"/api/desktop/telemetry/sessions/{sid}/events",
                                           f"/api/desktop/telemetry/sessions/{sid}/summary"]


def test_client_error_response_and_retry_after(stub):
    Stub.answer = (429, {"error": "too many requests", "code": "rate_limited"}, {"Retry-After": "3"})
    resp = TelemetryClient(link_config(stub)).post_status({})
    assert resp.status == 429 and resp.code == "rate_limited" and resp.retry_after == 3.0


def test_client_does_not_follow_redirects(stub):
    Stub.answer = (307, {}, {"Location": "http://127.0.0.1:9/steal"})
    resp = TelemetryClient(link_config(stub)).post_status({})
    assert resp.status == 307 and len(Stub.seen) == 1


def test_client_network_error_has_no_token():
    client = TelemetryClient(link_config("http://127.0.0.1:9"), timeout=1.0)
    with pytest.raises(NetworkError) as exc:
        client.post_status({})
    assert TOKEN not in str(exc.value) and "atcsdev_" not in repr(exc.value)
    assert exc.value.__cause__ is None  # the urllib error (with the request) is not chained


def test_probe_sends_no_document(stub):
    resp = TelemetryClient(link_config(stub)).probe()
    assert resp.status == 415
    _path, headers, body = Stub.seen[0]
    assert headers["Content-Type"] == "text/plain" and body == b"probe"


# ---------------------------------------------------------------------- CLI
def run_cli(monkeypatch, capsys, argv, stdin=""):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    rc = cli.main(argv)
    out = capsys.readouterr()
    return rc, out.out + out.err


@posix_only
@pytest.mark.parametrize("status,rc,text", [(415, 0, "OK"), (401, 1, "refused the device token"),
                                            (403, 1, "origin_refused"), (404, 1, "Unexpected answer")])
def test_cli_setup_and_check(home, stub, monkeypatch, capsys, status, rc, text):
    Stub.answer = (status, {"code": "x"}, {})
    code, out = run_cli(monkeypatch, capsys, ["setup", "--backend", stub, "--device", "alberto-pc", "--token-stdin"],
                        TOKEN + "\n")
    assert code == rc and text in out
    assert TOKEN not in out and "atcsdev_" not in out
    assert config.load_token().reveal() == TOKEN


def test_cli_setup_refuses_bad_input(home, monkeypatch, capsys):
    code, out = run_cli(monkeypatch, capsys, ["setup", "--backend", "http://10.1.1.1:5000", "--device", "abc",
                                              "--token-stdin", "--no-check"], TOKEN + "\n")
    assert code == 1 and "this computer" in out
    code, out = run_cli(monkeypatch, capsys, ["setup", "--device", "abc", "--token-stdin", "--no-check"],
                        "eyJhbGciOi.not-a-device-token\n")
    assert code == 1 and "not a device token" in out and "eyJhbGciOi" not in out
    assert not config.config_path().exists()


@posix_only
def test_cli_status_disable_forget(home, monkeypatch, capsys):
    run_cli(monkeypatch, capsys, ["setup", "--device", "alberto-pc", "--token-stdin", "--no-check"], TOKEN)
    code, out = run_cli(monkeypatch, capsys, ["status"])
    assert code == 0 and "alberto-pc" in out and "Token stored: yes" in out and TOKEN not in out
    run_cli(monkeypatch, capsys, ["disable"])
    assert config.load_config() is None
    run_cli(monkeypatch, capsys, ["enable"])
    assert config.load_config() is not None
    code, out = run_cli(monkeypatch, capsys, ["forget"])
    assert "device_token" in out and not config.token_path().exists()


def test_cli_check_when_not_set_up(home, monkeypatch, capsys):
    code, out = run_cli(monkeypatch, capsys, ["check"])
    assert code == 1 and "not set up" in out
