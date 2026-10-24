#!/usr/bin/env bash
# Launch ATCS Monitor from anywhere: ./start.sh [--video FILE [--start]]
set -uo pipefail
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PY="$ROOT/.venv/bin/python"

die() { echo "ATCS Monitor: $*" >&2; exit 1; }

# Keep third-party config/cache writes inside the project (same as app/paths.py).
export YOLO_CONFIG_DIR="${YOLO_CONFIG_DIR:-$ROOT/data/lib_config/ultralytics}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-$ROOT/data/lib_config/matplotlib}"
export YOLO_OFFLINE="${YOLO_OFFLINE:-true}"
export YOLO_AUTOINSTALL=false
mkdir -p "$YOLO_CONFIG_DIR" "$MPLCONFIGDIR"

[ -x "$PY" ] || die "virtual environment not found at $ROOT/.venv - run ./scripts/setup.sh first."

if ! "$PY" -c "import PySide6.QtWidgets, cv2, ultralytics, torch" 2>/dev/null; then
  die "dependencies are missing or broken in .venv - run ./scripts/setup.sh again.
$("$PY" -c "import PySide6.QtWidgets, cv2, ultralytics, torch" 2>&1 | tail -1)"
fi

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${QT_QPA_PLATFORM:-}" ]; then
  die "no graphical display found (DISPLAY / WAYLAND_DISPLAY are not set)."
fi

MODEL="$("$PY" -c "import sys; sys.path.insert(0, '$ROOT'); from app.settings import load_settings; print(load_settings().model_name)" 2>/dev/null || echo yolo26n.pt)"
if [ ! -f "$ROOT/models/$MODEL" ]; then
  echo "ATCS Monitor: note - models/$MODEL is missing; the app will offer to download it on Start." >&2
fi

cd "$ROOT"
exec "$PY" -m app.main "$@"
