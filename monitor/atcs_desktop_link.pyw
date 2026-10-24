"""Linked ATCS Monitor: the fixed program behind atcs-monitor://open (docs/desktop_link.md).

Windows runs it as: "<.venv-win>\\Scripts\\pythonw.exe" "<this file>" "%1"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.desktop_link.launcher import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
