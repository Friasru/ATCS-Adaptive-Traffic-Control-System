"""Path resolution for the three ways the app runs.

* development checkout - everything below the repository folder
* portable build (dist/...) - everything next to the executable
* installed (Windows installer) - the installer writes INSTALL_MARKER next to the
  executable; writable data then goes to a per-user folder
  (%LOCALAPPDATA%\\ATCS Monitor on Windows), because the program folder may be
  read-only and is shared by all users. Bundled model weights stay in the
  program folder and are found through model_dirs().

The data root can be overridden with the ATCS_MONITOR_HOME environment variable,
which the test-suite uses to isolate its writes in a temporary directory.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "ATCS Monitor"
INSTALL_MARKER = "installed.marker"


def install_dir() -> Path:
    """Folder holding the program (the executable's folder when frozen)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


_installed: dict[Path, bool] = {}


def is_installed() -> bool:
    """Decided once per process: if the uninstaller removes the marker while the app
    is still running, its last writes (settings saved on close) must still go to
    the per-user folder, never into the program folder."""
    if not getattr(sys, "frozen", False):
        return False
    d = install_dir()
    if d not in _installed:
        _installed[d] = (d / INSTALL_MARKER).is_file()
    return _installed[d]


def user_data_root() -> Path:
    """Per-user, writable application-data folder of an installed copy."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "atcs-monitor"


def project_root() -> Path:
    """Root of all writable data: db, settings, logs, recordings, exports, downloaded models."""
    override = os.environ.get("ATCS_MONITOR_HOME")
    if override:
        return Path(override).resolve()
    if is_installed():
        return user_data_root()
    # Development checkout, or a portable PyInstaller onedir build whose data
    # stays next to the program.
    return install_dir()


def bundled_root() -> Path:
    """Location of read-only resources shipped with the program."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(__file__).resolve().parent.parent


def models_dir() -> Path:
    """Writable models folder (downloads go here)."""
    return _ensure(project_root() / "models")


def model_dirs() -> list[Path]:
    """Folders searched for weights: the writable one first, then (frozen builds
    only) the weights shipped in the program folder."""
    dirs = [models_dir()]
    bundled = install_dir() / "models"
    if getattr(sys, "frozen", False) and bundled.is_dir() and bundled.resolve() != dirs[0].resolve():
        dirs.append(bundled)
    return dirs


def find_model(name: str) -> Path:
    """Path of the weights file `name`; the writable location if it exists nowhere."""
    for d in model_dirs():
        if (d / name).is_file():
            return d / name
    return models_dir() / name


def available_models() -> list[str]:
    return sorted({p.name for d in model_dirs() for p in d.glob("*.pt")})


def resource_path(name: str) -> Path:
    """Read-only file shipped with the program (app/resources)."""
    return bundled_root() / "app" / "resources" / name


def recordings_dir() -> Path:
    return _ensure(project_root() / "recordings")


def data_dir() -> Path:
    return _ensure(project_root() / "data")


def exports_dir() -> Path:
    return _ensure(project_root() / "exports")


def logs_dir() -> Path:
    return _ensure(data_dir() / "logs")


def database_path() -> Path:
    return data_dir() / "atcs_monitor.sqlite3"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def _ensure(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def configure_third_party_dirs() -> None:
    """Keep library config/cache writes inside the project.

    Must run before ultralytics / matplotlib are imported.
    """
    cfg = _ensure(data_dir() / "lib_config")
    os.environ.setdefault("YOLO_CONFIG_DIR", str(_ensure(cfg / "ultralytics")))
    os.environ.setdefault("MPLCONFIGDIR", str(_ensure(cfg / "matplotlib")))
    # Ultralytics must never try to pip-install anything at runtime.
    os.environ.setdefault("YOLO_AUTOINSTALL", "false")
    os.environ.setdefault("YOLO_OFFLINE", "true")
