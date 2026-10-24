import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Third-party config stays inside the project; tests never write to ~/.config.
os.environ.setdefault("YOLO_CONFIG_DIR", str(ROOT / "data" / "lib_config" / "ultralytics"))
os.environ.setdefault("MPLCONFIGDIR", str(ROOT / "data" / "lib_config" / "matplotlib"))
os.environ.setdefault("YOLO_OFFLINE", "true")
os.environ.setdefault("YOLO_AUTOINSTALL", "false")

MODELS = ROOT / "models"
MEDIA = ROOT / "data" / "test_media"
DEFAULT_MODEL = "yolo26n.pt"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate every project-local write (db, recordings, exports) in tmp."""
    monkeypatch.setenv("ATCS_MONITOR_HOME", str(tmp_path))
    return tmp_path


def require_model(name: str = DEFAULT_MODEL) -> Path:
    p = MODELS / name
    if not p.is_file():
        pytest.skip(f"model weights {p} not present (run scripts/download_model.py)")
    return p


def require_media(name: str) -> Path:
    p = MEDIA / name
    if not p.is_file():
        pytest.skip(f"test video {p} not present (run scripts/fetch_test_media.sh)")
    return p

# GUI tests run on Qt's offscreen platform (no real display needed).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def model_home(home):
    """Isolated project home whose models/ links to the real weights."""
    require_model()
    (home / "models").mkdir(exist_ok=True)
    for w in MODELS.glob("*.pt"):
        try:
            (home / "models" / w.name).symlink_to(w)
        except OSError:  # Windows without Developer Mode cannot create symlinks
            os.link(w, home / "models" / w.name)
    return home
