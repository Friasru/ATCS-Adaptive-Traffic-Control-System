"""Desktop link administration, run from the monitor folder with the app's Python:

    python -m app.desktop_link setup --backend http://127.0.0.1:5000 --device <name>
    python -m app.desktop_link check
    python -m app.desktop_link status
    python -m app.desktop_link retry [SESSION_ID]
    python -m app.desktop_link disable | enable
    python -m app.desktop_link forget
    python -m app.desktop_link where

`setup` asks for the device token without echoing it (or reads it from stdin
with --token-stdin). The token is never printed by any command.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import datetime

from app.desktop_link import config
from app.desktop_link.client import NetworkError, TelemetryClient

PROBE_RESULTS = {
    415: (0, "OK: the backend accepted this device's token and network origin."),
    401: (1, "The backend refused the device token (wrong, revoked or for another backend). "
             "Create a new one with monitor_access.py device add and run setup again."),
    403: (1, "The backend refused this computer's connection (origin_refused). From Windows, use "
             "http://127.0.0.1:<port>; see 'Network origins' in docs/desktop_telemetry.md."),
    429: (1, "The backend is rate limiting this device; try again in a few seconds."),
}


def _client() -> TelemetryClient:
    cfg = config.load_config()
    if cfg is None:
        raise config.ConfigError("the desktop link is not set up or is disabled")
    return TelemetryClient(cfg)


def cmd_check(_args) -> int:
    try:
        client = _client()
        resp = client.probe()
    except config.ConfigError as exc:
        print(f"Not ready: {exc}")
        return 1
    except NetworkError as exc:
        print(f"Cannot reach the backend: {exc}. Is Flask running (venv/bin/python run.py)?")
        return 1
    code, text = PROBE_RESULTS.get(resp.status, (1, f"Unexpected answer: HTTP {resp.status} {resp.code}. "
                                                    "Is this address the ATCS backend?"))
    print(text)
    return code


def cmd_setup(args) -> int:
    try:
        url = config.validate_backend_url(args.backend)
        name = config.validate_device_name(args.device)
        raw = sys.stdin.readline() if args.token_stdin else getpass.getpass("Device token (input hidden): ")
        token = config.validate_token(raw)
        config.save_token(token)
        path = config.save_config(url, name, enabled=True)
    except config.ConfigError as exc:
        print(f"Setup failed: {exc}")
        return 1
    print(f"Saved the link settings to {path}")
    print(f"Saved the device token to {config.token_path()} "
          + ("(encrypted for this Windows user)" if sys.platform == "win32" else "(readable by you only)"))
    if args.no_check:
        return 0
    return cmd_check(args)


def _set_enabled(enabled: bool) -> int:
    try:
        doc = config.read_config_file()
    except config.ConfigError as exc:
        print(f"Cannot change it: {exc}")
        return 1
    if doc is None:
        print("The desktop link is not set up.")
        return 1
    config.save_config(doc["backend_url"], doc["device_name"], enabled=enabled)
    print(f"Desktop link {'enabled' if enabled else 'disabled'}; it applies the next time the app starts.")
    return 0


def _when(t) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S") if t else "-"


def cmd_status(_args) -> int:
    from app.desktop_link.outbox import Outbox

    try:
        doc = config.read_config_file()
    except config.ConfigError as exc:
        print(f"Configuration problem: {exc}")
        doc = None
    if doc:
        print(f"Backend: {doc['backend_url']}  device: {doc['device_name']}  "
              f"{'enabled' if doc['enabled'] else 'DISABLED'}")
        print(f"Token stored: {'yes' if config.token_path().is_file() else 'NO'}")
    else:
        print("The desktop link is not set up (standalone mode).")
    if not config.outbox_path().is_file():
        print("Outbox: empty")
        return 0
    box = Outbox(config.outbox_path())
    try:
        counts = box.counts()
        print("Outbox: {pending} waiting, {open} not finished yet, {done} sent, {rejected} refused".format(**counts))
        for r in box.rows():
            line = f"  {r['session_id']}  {r['state']:<8} first seen {_when(r['first_seen_at'])}"
            if r["state"] == "pending":
                line += f"  events {r['events_sent']}/{r['events_total']}, attempts {r['attempts']}"
                if r["next_attempt_at"]:
                    line += f", next try {_when(r['next_attempt_at'])}"
            if r["last_code"]:
                line += f"  [{r['last_code']}] {r['last_error'] or ''}"
            print(line)
    finally:
        box.close()
    return 0


def cmd_retry(args) -> int:
    from app.desktop_link.outbox import Outbox

    box = Outbox(config.outbox_path())
    try:
        n = box.retry_rejected(args.session_id)
    finally:
        box.close()
    print(f"{n} refused session(s) queued again; they are sent while the linked app runs.")
    return 0


def cmd_forget(_args) -> int:
    removed = []
    for p in (config.token_path(), config.config_path()):
        if p.is_file():
            p.unlink()
            removed.append(p.name)
    print("Removed: " + (", ".join(removed) if removed else "nothing") + ". The outbox was kept.")
    return 0


def cmd_where(_args) -> int:
    print(config.link_dir())
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.desktop_link", description="ATCS desktop link administration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("setup", help="store the backend address and device token")
    s.add_argument("--backend", default="http://127.0.0.1:5000", help="ATCS backend on this computer")
    s.add_argument("--device", required=True, help="device name given to monitor_access.py device add")
    s.add_argument("--token-stdin", action="store_true", help="read the token from standard input")
    s.add_argument("--no-check", action="store_true", help="do not contact the backend")
    s.set_defaults(func=cmd_setup)
    sub.add_parser("check", help="test the address and token without sending data").set_defaults(func=cmd_check)
    sub.add_parser("status", help="show the configuration and the upload queue").set_defaults(func=cmd_status)
    r = sub.add_parser("retry", help="queue refused sessions again")
    r.add_argument("session_id", nargs="?")
    r.set_defaults(func=cmd_retry)
    sub.add_parser("disable", help="run standalone from the next start").set_defaults(func=lambda a: _set_enabled(False))
    sub.add_parser("enable", help="link again from the next start").set_defaults(func=lambda a: _set_enabled(True))
    sub.add_parser("forget", help="delete the stored token and settings").set_defaults(func=cmd_forget)
    sub.add_parser("where", help="print the link folder").set_defaults(func=cmd_where)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
