"""Give existing accounts access to the monitor endpoints, and register desktop monitors.

    venv/bin/python monitor_access.py grant <username> viewer|operator
    venv/bin/python monitor_access.py revoke <username>
    venv/bin/python monitor_access.py list

    venv/bin/python monitor_access.py device add <name>      # prints the device token once
    venv/bin/python monitor_access.py device revoke <name>
    venv/bin/python monitor_access.py device list

Run it on the server, against the same database as the web app. A grant is tied
to that exact account: if the account is registered again (for example after
atcs.db was recreated), the old grant no longer applies and must be given again.

A device token lets one desktop ATCS Monitor send telemetry
(monitor/docs/desktop_telemetry.md). Only its hash is stored, so it is shown
once, when the device is added; if it is lost, revoke the device and add a new one.
"""

import argparse
import secrets
import sys

from app import create_app
from app.services import monitor_access, monitor_devices
from config import Config


class ScriptConfig(Config):
    # The script never issues tokens; a missing secret is replaced for this process only.
    JWT_SECRET_KEY = Config.JWT_SECRET_KEY or secrets.token_hex(32)


def main(argv=None, config_class=ScriptConfig):
    ap = argparse.ArgumentParser(description="Manage ATCS monitor access")
    sub = ap.add_subparsers(dest="command", required=True)
    g = sub.add_parser("grant", help="give an existing account viewer or operator access")
    g.add_argument("username")
    g.add_argument("role", choices=["viewer", "operator"])
    r = sub.add_parser("revoke", help="remove an account's access")
    r.add_argument("username")
    sub.add_parser("list", help="show all grants")
    d = sub.add_parser("device", help="register, revoke or list desktop monitors")
    dsub = d.add_subparsers(dest="device_command", required=True)
    dsub.add_parser("add", help="register a desktop monitor and print its token once").add_argument("name")
    dsub.add_parser("revoke", help="revoke a desktop monitor; its token stops working").add_argument("name")
    dsub.add_parser("list", help="show registered desktop monitors")
    args = ap.parse_args(argv)

    app = create_app(config_class)
    with app.app_context():
        if args.command == "grant":
            try:
                monitor_access.grant(args.username, args.role)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"{args.username}: {args.role}")
        elif args.command == "revoke":
            print(f"removed {monitor_access.revoke(args.username)} grant(s)")
        elif args.command == "device":
            return device_command(args)
        else:
            for grant, valid in monitor_access.list_grants():
                note = "" if valid else "  (INVALID: account changed or missing; grant again or revoke)"
                print(f"{grant['username']:<24} {grant['role']:<9} {grant['granted_at']}{note}")
    return 0


def device_command(args):
    if args.device_command == "add":
        try:
            token = monitor_devices.add(args.name)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"device {args.name!r} registered. Its token is shown only this once:")
        print()
        print(f"    {token}")
        print()
        print("Put it in the desktop monitor's link configuration. Do not commit it or paste it into a browser.")
    elif args.device_command == "revoke":
        if not monitor_devices.revoke(args.name):
            print(f"error: no active device named {args.name!r}", file=sys.stderr)
            return 1
        print(f"device {args.name!r} revoked")
    else:
        for dev in monitor_devices.list_devices():
            state = "active " if dev["active"] else "REVOKED"
            print(f"{dev['name']:<24} {state} created {dev['created_at']}  last seen {dev['last_seen_at'] or 'never'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
