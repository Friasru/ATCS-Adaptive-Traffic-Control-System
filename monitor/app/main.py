"""ATCS Monitor entry point."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):  # allow `python app/main.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import APP_NAME, __version__, paths  # noqa: E402

log = logging.getLogger("atcs_monitor")

# Windows taskbar identity; the installer gives its shortcuts the same ID so pinned
# shortcuts and running windows group under the app's own icon.
APP_USER_MODEL_ID = "ATCSMonitor.Desktop"
# Named kernel objects shared with the installer (packaging/installer/atcs_monitor.iss):
# the mutex tells Setup/Uninstall the app is running; setting the event asks every
# running instance to close normally (as if its window were closed), so files can be
# replaced or removed without killing a recording.
RUNNING_MUTEX = "ATCSMonitor.Running"
CLOSE_EVENT = "ATCSMonitor.CloseRequest"


def watch_installer_close_requests(window) -> None:
    """Windows only: hold RUNNING_MUTEX and close `window` when CLOSE_EVENT is set."""
    import ctypes
    import threading
    from ctypes import wintypes

    from PySide6.QtCore import QMetaObject, Qt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.ResetEvent.argtypes = [wintypes.HANDLE]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    window._win_mutex = k32.CreateMutexW(None, False, RUNNING_MUTEX)  # released when the process ends
    event = k32.CreateEventW(None, True, False, CLOSE_EVENT)  # manual reset: wakes every instance
    if not window._win_mutex or not event:
        log.warning("Installer close-request objects unavailable (error %d)", ctypes.get_last_error())
        return
    k32.ResetEvent(event)  # a request left over from an earlier session does not apply to this one

    def wait():
        if k32.WaitForSingleObject(event, 0xFFFFFFFF) == 0:
            log.info("The installer asked ATCS Monitor to close")
            QMetaObject.invokeMethod(window, "close", Qt.ConnectionType.QueuedConnection)

    threading.Thread(target=wait, name="installer-close", daemon=True).start()


def setup_logging(verbose: bool = False) -> Path:
    log_file = paths.logs_dir() / "atcs_monitor.log"
    handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.INFO if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)
    logging.getLogger("ultralytics").setLevel(logging.ERROR)  # silence per-frame chatter
    return log_file


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="atcs-monitor", description=f"{APP_NAME} {__version__}")
    ap.add_argument("--video", help="pre-select a video file as source")
    ap.add_argument("--camera", type=int, metavar="INDEX", help="pre-select the discovered camera with this index")
    ap.add_argument("--start", action="store_true", help="start monitoring immediately")
    ap.add_argument("--record", action="store_true", help="with --start: begin recording as soon as detection runs")
    ap.add_argument("--quit-after", type=int, default=0, metavar="MS",
                    help="close the window after MS milliseconds (launch checks)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    # A windowed (console=False) Windows build has no stdout/stderr; libraries that
    # write progress there (tqdm, print) would otherwise raise.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))

    paths.configure_third_party_dirs()
    log_file = setup_logging(args.verbose)
    log.info("%s %s starting (data=%s, program=%s, installed=%s)", APP_NAME, __version__, paths.project_root(),
             paths.install_dir(), paths.is_installed())

    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.settings import load_settings
    from app.storage.repository import Repository, StorageError
    from app.ui.main_window import MainWindow

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        except (AttributeError, OSError):
            log.warning("Could not set the Windows AppUserModelID")
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    icon = paths.resource_path("atcs_monitor.ico" if sys.platform == "win32" else "atcs_monitor.png")
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))

    def excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        QMessageBox.critical(None, APP_NAME, f"Unexpected error: {exc}\n\nDetails were written to {log_file}")

    sys.excepthook = excepthook

    try:
        repo = Repository(paths.database_path())
        n = repo.mark_interrupted_sessions()
        if n:
            log.warning("%d session(s) from a previous run were not closed and are now marked 'interrupted'", n)
    except StorageError as exc:
        QMessageBox.critical(None, APP_NAME, f"Cannot open the local database:\n{exc}")
        return 2

    win = MainWindow(load_settings(), repo)
    win.show()
    if sys.platform == "win32":
        watch_installer_close_requests(win)

    def start_selected():
        QTimer.singleShot(300, win.start_monitoring)
        if args.record:
            from app.services.monitoring import State

            def record_when_ready():
                if win.state == State.DETECTING:
                    win.start_recording()
                elif win.state in (State.INITIALIZING, State.IDLE):
                    QTimer.singleShot(200, record_when_ready)

            QTimer.singleShot(500, record_when_ready)

    def select_camera_when_discovered():
        if win._discovering:
            QTimer.singleShot(200, select_camera_when_discovered)
            return
        combo = win.source_combo
        idx = next((i for i in range(combo.count()) if (combo.itemData(i) or ("",))[0] == "camera"
                    and combo.itemData(i)[1] == args.camera), -1)
        if idx < 0:
            log.error("--camera %d: no such camera was discovered", args.camera)
            win.statusBar().showMessage(f"Camera #{args.camera} was not found", 10000)
            return
        combo.setCurrentIndex(idx)
        if args.start:
            start_selected()

    if args.video:
        win.add_video_source(args.video)
        if args.start:
            start_selected()
    elif args.camera is not None:
        QTimer.singleShot(0, select_camera_when_discovered)
    if args.quit_after > 0:
        QTimer.singleShot(args.quit_after, win.close)
    rc = app.exec()
    # Destroy Qt objects deterministically before the interpreter tears down
    # (otherwise PySide6 may finalize widgets after QApplication is gone).
    win.deleteLater()
    del win
    app.processEvents()
    repo.close()
    log.info("%s exited with code %s", APP_NAME, rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
