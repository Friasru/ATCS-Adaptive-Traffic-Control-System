#!/usr/bin/env python3
"""Download official Ultralytics YOLO weights into the project's models/ folder.

Usage: .venv/bin/python scripts/download_model.py [yolo26n.pt] [yolo11n.pt ...]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402

paths.configure_third_party_dirs()
os.environ["YOLO_OFFLINE"] = "false"  # this script is the one place allowed to go online

from app.detection.detector import ModelError, download_weights, file_sha256  # noqa: E402
from app.settings import Settings  # noqa: E402


def main(argv: list[str]) -> int:
    names = argv or [Settings().model_name]
    rc = 0
    for name in names:
        try:
            p = download_weights(name)
            print(f"OK  {p}  ({p.stat().st_size / 1e6:.1f} MB, sha256:{file_sha256(p)})")
        except ModelError as exc:
            print(f"ERR {exc}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
