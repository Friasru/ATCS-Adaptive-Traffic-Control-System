"""Give existing accounts access to the monitor endpoints.

    venv/bin/python monitor_access.py grant <username> viewer|operator
    venv/bin/python monitor_access.py revoke <username>
    venv/bin/python monitor_access.py list

Run it on the server, against the same database as the web app. A grant is tied
to that exact account: if the account is registered again (for example after
atcs.db was recreated), the old grant no longer applies and must be given again.
"""

import argparse
import secrets
import sys

from app import create_app
from app.services import monitor_access
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
        else:
            for grant, valid in monitor_access.list_grants():
                note = "" if valid else "  (INVALID: account changed or missing; grant again or revoke)"
                print(f"{grant['username']:<24} {grant['role']:<9} {grant['granted_at']}{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
