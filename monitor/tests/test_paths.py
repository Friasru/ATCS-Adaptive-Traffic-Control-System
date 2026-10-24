"""Data locations for the development checkout, the portable build and an installed copy."""

import sys
from pathlib import Path

import pytest

from app import paths

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Pretend to be a PyInstaller build whose program folder is tmp/prog."""
    prog = tmp_path / "prog"
    (prog / "models").mkdir(parents=True)
    (prog / "models" / "yolo26n.pt").write_bytes(b"bundled")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(prog / "atcs-monitor.exe"))
    monkeypatch.delenv("ATCS_MONITOR_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "local"))
    monkeypatch.setattr(paths, "_installed", {})
    return prog


def test_development_checkout_uses_the_repository(monkeypatch):
    monkeypatch.delenv("ATCS_MONITOR_HOME", raising=False)
    assert not getattr(sys, "frozen", False)
    assert paths.project_root() == REPO
    assert not paths.is_installed()
    assert paths.model_dirs() == [REPO / "models"]


def test_portable_build_keeps_data_next_to_the_executable(frozen):
    assert not paths.is_installed()
    assert paths.project_root() == frozen
    assert paths.recordings_dir() == frozen / "recordings"
    assert paths.find_model("yolo26n.pt") == frozen / "models" / "yolo26n.pt"


def test_installed_copy_writes_to_the_per_user_folder(frozen, tmp_path):
    (frozen / paths.INSTALL_MARKER).write_text("installed")
    user = paths.user_data_root()
    assert user.parent == tmp_path / "local"
    assert paths.is_installed()
    assert paths.project_root() == user
    for d in (paths.recordings_dir(), paths.exports_dir(), paths.data_dir(), paths.logs_dir(), paths.models_dir()):
        assert d.is_dir() and user in d.parents
    assert paths.database_path().parent == user / "data"
    assert paths.settings_path().parent == user / "data"
    assert not any(frozen.glob("recordings")), "nothing may be written into the program folder"


def test_installed_copy_finds_bundled_weights_and_prefers_user_downloads(frozen):
    (frozen / paths.INSTALL_MARKER).write_text("installed")
    assert paths.find_model("yolo26n.pt") == frozen / "models" / "yolo26n.pt"
    assert paths.find_model("yolo11n.pt") == paths.models_dir() / "yolo11n.pt"  # where a download would go
    (paths.models_dir() / "yolo26n.pt").write_bytes(b"user copy")
    assert paths.find_model("yolo26n.pt") == paths.models_dir() / "yolo26n.pt"
    (paths.models_dir() / "yolo11n.pt").write_bytes(b"downloaded")
    assert paths.available_models() == ["yolo11n.pt", "yolo26n.pt"]


def test_installed_mode_does_not_change_while_running(frozen):
    """The uninstaller can delete the marker while the app still runs: the data
    location must stay the per-user folder."""
    marker = frozen / paths.INSTALL_MARKER
    marker.write_text("installed")
    user = paths.project_root()
    marker.unlink()
    assert paths.is_installed() and paths.project_root() == user
    assert paths.settings_path().parent == user / "data"


def test_override_wins_over_installed_mode(frozen, tmp_path, monkeypatch):
    (frozen / paths.INSTALL_MARKER).write_text("installed")
    monkeypatch.setenv("ATCS_MONITOR_HOME", str(tmp_path / "elsewhere"))
    assert paths.project_root() == (tmp_path / "elsewhere").resolve()


def test_icon_resources_are_shipped():
    for name in ("atcs_monitor.ico", "atcs_monitor.png"):
        assert paths.resource_path(name).is_file()
