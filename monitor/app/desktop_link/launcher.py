"""Entry point of the linked desktop app (atcs_desktop_link.pyw).

Started by hand, or by Windows for an atcs-monitor://open link after the user
confirmed the browser prompt. Steps:

1. the arguments must be nothing, or exactly one accepted URI (uri.py);
   anything else exits with code 2 and opens nothing;
2. if a linked app already runs for this user, it is asked to show its window
   and this process exits; if another ATCS Monitor (for example the installed
   one) runs, its window is raised and this process exits. Nothing is killed;
3. otherwise the normal app starts with fixed arguments: no source is selected
   and monitoring is not started. The link attaches to its window if
   desktop_link.json enables it; without it the app runs standalone.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Callable, Optional, Sequence

from app.desktop_link import uri
from app.desktop_link.instance import InstanceGuard, activate_running, focus_window_by_title, original_app_running

log = logging.getLogger("atcs_monitor.desktop_link")


def _launch_log() -> logging.Logger:
    """A small log of launch decisions, written before the app's own logging exists."""
    from app.desktop_link.config import link_dir

    lg = logging.getLogger("atcs_monitor.desktop_link.launch")
    path = str((link_dir() / "launcher.log").resolve())
    for h in list(lg.handlers):
        if getattr(h, "baseFilename", None) != path:
            lg.removeHandler(h)
            h.close()
    if not lg.handlers:
        h = logging.handlers.RotatingFileHandler(path, maxBytes=200_000, backupCount=1, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


def attach(window) -> Optional[Callable[[], None]]:
    """window_hook for app.main: start the link if it is configured. Returns its cleanup."""
    from PySide6.QtCore import QCoreApplication
    from PySide6.QtWidgets import QLabel

    from app import __version__
    from app.desktop_link import config
    from app.desktop_link.outbox import Outbox
    from app.desktop_link.qt_link import DesktopLink
    from app.desktop_link.service import LinkService

    try:
        cfg = config.load_config()
    except config.ConfigError as exc:
        log.error("Desktop link not started: %s", exc)
        label = QLabel("ATCS: link not set up")
        label.setToolTip(str(exc))
        window.statusBar().addPermanentWidget(label)
        return None
    if cfg is None:
        log.info("Desktop link is not configured or disabled; running standalone")
        return None
    outbox = Outbox(config.outbox_path())
    service = LinkService(cfg, window.repo, outbox, __version__)
    link = DesktopLink(window, service)
    service.start()

    def cleanup() -> None:
        QCoreApplication.processEvents()  # deliver the last engine signals
        rep = window.engine.last_report
        if rep is not None:
            service.session_finished(rep.session_id)
        link.tick()
        link.close()
        service.stop(flush_s=3.0)
        service.join(15)
        outbox.close()

    return cleanup


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    launch = _launch_log()
    try:
        uri.check_argv(argv)
    except uri.LaunchRejected as exc:
        launch.warning("Launch refused: %s (%d argument(s), first: %.80r)", exc, len(argv), argv[0] if argv else "")
        return 2

    guard = InstanceGuard()
    if not guard.acquire():
        if activate_running():
            launch.info("Linked ATCS Monitor already running; asked it to show its window")
        elif focus_window_by_title():
            launch.info("Linked ATCS Monitor already running; raised its window")
        else:
            launch.info("Linked ATCS Monitor already running; its window could not be raised")
        return 0
    try:
        if original_app_running():
            raised = focus_window_by_title()
            launch.info("Another ATCS Monitor is running; not starting a second one (window raised: %s)", raised)
            return 0
        launch.info("Starting linked ATCS Monitor (%s)", "browser link" if argv else "started directly")
        from app.main import main as app_main

        return app_main([], window_hook=attach)
    finally:
        guard.release()
