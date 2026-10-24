"""One linked ATCS Monitor per user, and bringing the running one to the front.

* InstanceGuard is taken before anything else starts. On Windows it is a named
  mutex in the user's session (released by Windows when the process ends, even
  after a crash); elsewhere an exclusive lock on a file in the link folder.
* original_app_running() detects any ATCS Monitor window process, including the
  separately installed app, through the mutex every ATCS Monitor holds while it
  runs (app/main.py RUNNING_MUTEX). The linked app then does not start: a
  second engine would compete for the same camera.
* A running linked app listens on a local socket (qt_link) for "activate"; when
  the running app is the installed one, its window is found by title instead.

Nothing here ever closes or kills another process.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger("atcs_monitor.desktop_link")

LINK_MUTEX = "Local\\ATCSMonitor.DesktopLink"
RUNNING_MUTEX = "ATCSMonitor.Running"  # same name as app.main.RUNNING_MUTEX
WINDOW_TITLE = "ATCS Monitor"
ACTIVATE = b"activate\n"
ERROR_ALREADY_EXISTS = 183


def activation_name() -> str:
    """Local socket name, distinct per user account."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return "atcs-monitor-link-" + hashlib.sha256(user.encode("utf-8", "replace")).hexdigest()[:16]


class InstanceGuard:
    def __init__(self, lock_dir: Optional[Path] = None):
        self._lock_dir = lock_dir
        self._handle = None
        self._fh = None

    def acquire(self) -> bool:
        """True if this process is now the only linked instance."""
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = wintypes.HANDLE
            k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = k32.CreateMutexW(None, False, LINK_MUTEX)
            err = ctypes.get_last_error()
            if not handle:
                log.warning("Could not create the instance mutex (error %d)", err)
                return True  # cannot tell; the RUNNING_MUTEX check still applies
            if err == ERROR_ALREADY_EXISTS:
                k32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        import fcntl

        from app.desktop_link.config import link_dir

        path = (self._lock_dir or link_dir()) / "instance.lock"
        fh = open(path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def release(self) -> None:
        if self._handle is not None:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32")
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle(self._handle)
            self._handle = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def original_app_running() -> bool:
    """True if any ATCS Monitor window process runs for this user (Windows only)."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = wintypes.HANDLE
    k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.OpenMutexW(0x00100000, False, RUNNING_MUTEX)  # SYNCHRONIZE
    if handle:
        k32.CloseHandle(handle)
        return True
    return False


def allow_foreground() -> None:
    """Let the running instance take the foreground (this process was just started by a click)."""
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.WinDLL("user32").AllowSetForegroundWindow(-1)  # ASFW_ANY
        except (AttributeError, OSError):
            pass


def focus_window_by_title(title: str = WINDOW_TITLE) -> bool:
    """Best effort: restore and raise a top-level window with this exact title (Windows only).

    Windows may refuse to move another process's window to the front; it then
    flashes the taskbar button instead. Returns False if no such window exists.
    """
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    return True


def activate_running(timeout_ms: int = 1500) -> bool:
    """Ask a running linked app to show its window. False if none answered."""
    try:
        from PySide6.QtNetwork import QLocalSocket
    except ImportError:
        return False
    allow_foreground()
    sock = QLocalSocket()
    sock.connectToServer(activation_name())
    if not sock.waitForConnected(timeout_ms):
        return False
    sock.write(ACTIVATE)
    sock.flush()
    # A Windows pipe write completes only when the running app reads it; if it does not (its
    # window is busy), disconnecting would drop the request, so report failure instead and let
    # the launcher raise the window by its title.
    if sock.bytesToWrite() and not sock.waitForBytesWritten(timeout_ms):
        sock.abort()
        return False
    sock.disconnectFromServer()
    return True
