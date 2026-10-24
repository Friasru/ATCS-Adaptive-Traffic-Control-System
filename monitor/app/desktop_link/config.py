"""Link configuration and the device token.

Everything lives in <data>/desktop_link/ (paths.data_dir()), i.e. in the
checkout's data/ folder for a development copy, in %LOCALAPPDATA%\\ATCS Monitor
for an installed one, or under ATCS_MONITOR_HOME when that is set:

* desktop_link.json - {"enabled", "backend_url", "device_name"}; no secrets
* device_token      - the token issued by `monitor_access.py device add`.
  On Windows it is encrypted with DPAPI for the current Windows user (another
  account, or a copy of the file on another computer, cannot decrypt it). On
  other systems the file must be readable by its owner only (mode 0600).

The token is wrapped in Secret, whose str/repr never show the value, and no
function here puts it in a log line or an exception message.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from app import paths

CONFIG_NAME = "desktop_link.json"
TOKEN_NAME = "device_token"
TOKEN_RE = re.compile(r"atcsdev_[A-Za-z0-9_\-]{43}")
DEVICE_NAME_RE = re.compile(r"[A-Za-z0-9._\-]{3,64}")
# The backend accepts telemetry from loopback clients only (desktop_telemetry.md
# section 4), so the token is never sent anywhere else either.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
DPAPI_ENTROPY = b"ATCS Monitor desktop link"


class ConfigError(ValueError):
    pass


class Secret:
    """A value that never appears in str(), repr(), logs or tracebacks."""

    __slots__ = ("_value",)

    def __init__(self, value: str):
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    __str__ = __repr__

    def __eq__(self, other) -> bool:
        return isinstance(other, Secret) and other._value == self._value

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass
class LinkConfig:
    backend_url: str
    device_name: str
    token: Secret = field(repr=False)
    enabled: bool = True


def link_dir() -> Path:
    d = paths.data_dir() / "desktop_link"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return link_dir() / CONFIG_NAME


def token_path() -> Path:
    return link_dir() / TOKEN_NAME


def outbox_path() -> Path:
    return link_dir() / "outbox.sqlite3"


# ------------------------------------------------------------- validation
def validate_backend_url(url: object) -> str:
    """http(s)://<loopback>[:port] with nothing else. Returns the normalized URL."""
    if not isinstance(url, str) or len(url) > 100:
        raise ConfigError("backend_url must be a short http://127.0.0.1:<port> address")
    try:
        p = urlsplit(url.strip())
        port = p.port
    except ValueError as exc:
        raise ConfigError("backend_url is not a valid URL") from exc
    if p.scheme not in ("http", "https"):
        raise ConfigError("backend_url must start with http:// or https://")
    if p.username or p.password or p.query or p.fragment or p.path not in ("", "/"):
        raise ConfigError("backend_url must be only scheme, host and port")
    host = (p.hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise ConfigError("backend_url must point to this computer (127.0.0.1, localhost or ::1)")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return f"{p.scheme}://{netloc}"


def validate_device_name(name: object) -> str:
    if not isinstance(name, str) or not DEVICE_NAME_RE.fullmatch(name):
        raise ConfigError("device_name must be 3 to 64 letters, digits, '.', '_' or '-'")
    return name


def validate_token(token: object) -> Secret:
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token.strip()):
        raise ConfigError("that is not a device token (expected atcsdev_ followed by 43 characters)")
    return Secret(token.strip())


# ------------------------------------------------------------ token store
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    src, _keep1 = blob(data)
    ent, _keep2 = blob(DPAPI_ENTROPY)
    out = Blob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(src), "ATCS device token", ctypes.byref(ent), None, None, flags,
                                      ctypes.byref(out))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, flags,
                                        ctypes.byref(out))
    if not ok:
        raise ConfigError(f"Windows could not {'protect' if protect else 'read'} the device token "
                          f"(error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(text)
    os.replace(tmp, path)


def save_token(token: Secret, path: Optional[Path] = None) -> Path:
    path = path or token_path()
    if sys.platform == "win32":
        text = "dpapi:" + base64.b64encode(_dpapi(token.reveal().encode("ascii"), True)).decode("ascii")
    else:
        text = "plain:" + token.reveal()
    _write_private(path, text + "\n")
    return path


def load_token(path: Optional[Path] = None) -> Secret:
    path = path or token_path()
    if not path.is_file():
        raise ConfigError("no device token is stored (run: python -m app.desktop_link setup)")
    if sys.platform != "win32":
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(f"{path.name} can be read by other users; run: chmod 600 {path}")
    text = path.read_text(encoding="ascii", errors="replace").strip()
    kind, _, body = text.partition(":")
    if kind == "dpapi":
        if sys.platform != "win32":
            raise ConfigError("the stored token is encrypted for Windows; set it up again on this system")
        try:
            value = _dpapi(base64.b64decode(body, validate=True), False).decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ConfigError("the stored device token is damaged; set it up again") from exc
    elif kind == "plain":
        if sys.platform == "win32":
            raise ConfigError("the stored token is not protected; set it up again on Windows")
        value = body
    else:
        raise ConfigError("the stored device token is damaged; set it up again")
    try:
        return validate_token(value)
    except ConfigError:
        raise ConfigError("the stored device token is damaged; set it up again") from None


# ----------------------------------------------------------------- config
def save_config(backend_url: str, device_name: str, enabled: bool = True, path: Optional[Path] = None) -> Path:
    path = path or config_path()
    doc = {"enabled": bool(enabled), "backend_url": validate_backend_url(backend_url),
           "device_name": validate_device_name(device_name)}
    _write_private(path, json.dumps(doc, indent=2) + "\n")
    return path


def read_config_file(path: Optional[Path] = None) -> Optional[dict]:
    """The JSON settings (no token), or None if the link was never set up."""
    path = path or config_path()
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path.name} is not valid JSON") from exc
    if not isinstance(doc, dict) or set(doc) - {"enabled", "backend_url", "device_name"}:
        raise ConfigError(f"{path.name} has unexpected content")
    if not isinstance(doc.get("enabled", True), bool):
        raise ConfigError("'enabled' must be true or false")
    return {"enabled": doc.get("enabled", True), "backend_url": validate_backend_url(doc.get("backend_url")),
            "device_name": validate_device_name(doc.get("device_name"))}


def load_config() -> Optional[LinkConfig]:
    """The link configuration, or None when the link is not set up or is disabled.

    Raises ConfigError when it is set up but unusable (bad file, missing token).
    """
    doc = read_config_file()
    if doc is None or not doc["enabled"]:
        return None
    return LinkConfig(backend_url=doc["backend_url"], device_name=doc["device_name"], token=load_token(),
                      enabled=True)
