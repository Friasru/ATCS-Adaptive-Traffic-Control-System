"""The launcher URI. Only one action exists: open (or focus) the desktop app.

The browser passes the clicked URI as the only argument. Everything about it is
fixed: no query, fragment, path, user info, port or extra arguments is
accepted, so nothing the browser sends can select a file, a camera, a program
or start monitoring.
"""

from __future__ import annotations

from typing import Sequence

SCHEME = "atcs-monitor"
OPEN_URI = f"{SCHEME}://open"
ACCEPTED = (OPEN_URI, OPEN_URI + "/")
MAX_LEN = 64


class LaunchRejected(ValueError):
    pass


def parse(uri: object) -> str:
    """Return "open" for an accepted URI; raise LaunchRejected otherwise.

    The scheme is compared case-insensitively (RFC 3986); the rest must match
    exactly. No decoding or normalisation is done, so encoded tricks such as
    %2F or a trailing "?" never match.
    """
    if not isinstance(uri, str) or not uri or len(uri) > MAX_LEN:
        raise LaunchRejected("unexpected launcher argument")
    scheme, sep, rest = uri.partition(":")
    if not sep or scheme.lower() != SCHEME or f"{SCHEME}:{rest}" not in ACCEPTED:
        raise LaunchRejected("unsupported launcher URI")
    return "open"


def check_argv(argv: Sequence[str]) -> str:
    """Arguments of the linked entry point: none (started by hand) or exactly one accepted URI."""
    if len(argv) == 0:
        return "open"
    if len(argv) != 1:
        raise LaunchRejected("the launcher accepts a single URI argument")
    return parse(argv[0])
