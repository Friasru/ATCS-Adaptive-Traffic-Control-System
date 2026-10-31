#!/usr/bin/env bash
# Launch the 3D simulator service that the ATCS "3D Simulator" page shows. Run it from anywhere:
#   simulation3d/run.sh                   serve on http://127.0.0.1:8766 (this computer only; the page connects by itself)
#   simulation3d/run.sh --port 9001       other port; then open simulator3d.html?port=9001
#   simulation3d/run.sh benchmark [--all] headless full-hour comparison of the three controllers (writes results/benchmark_*.json)
#   simulation3d/run.sh test [pytest args] this component's tests (add -m "not slow" to skip the full-hour runs)
# Python: $ATCS3D_PYTHON, else the repository's .venv, else backend/venv, else python3. Port: $ATCS3D_PORT (default 8766).
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
REPO="$(cd .. && pwd)"

PY="${ATCS3D_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in "$REPO/.venv/bin/python" "$REPO/backend/venv/bin/python"; do
    [ -x "$candidate" ] && { PY="$candidate"; break; }
  done
fi
PY="${PY:-python3}"

command -v sumo >/dev/null 2>&1 || { echo "SUMO is not installed (need 'sumo' on PATH, version 1.18 or newer). On Ubuntu: sudo apt install sumo sumo-tools"; exit 1; }
export SUMO_HOME="${SUMO_HOME:-/usr/share/sumo}"
export PYTHONDONTWRITEBYTECODE=1   # never leave bytecode caches inside simulation/
"$PY" -c "import aiohttp, traci, sumolib" 2>/dev/null || {
  echo "$PY cannot import aiohttp, traci and sumolib. Install them with: $PY -m pip install -r $REPO/simulation3d/requirements.txt -r $REPO/backend/requirements.txt"
  exit 1
}

case "${1:-}" in
  benchmark) shift; exec "$PY" -m atcs3d.benchmark "$@" ;;
  test)      shift; exec "$PY" -m pytest "$@" ;;
  *)         exec "$PY" -m atcs3d.server --port "${ATCS3D_PORT:-8766}" "$@" ;;
esac
