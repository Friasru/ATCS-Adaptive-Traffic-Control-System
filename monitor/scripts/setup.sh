#!/usr/bin/env bash
# Create the project-local virtual environment and install dependencies.
#   ./scripts/setup.sh          auto-detect NVIDIA GPU (CUDA 12.8 build) or CPU-only torch
#   ./scripts/setup.sh --cpu    force CPU-only torch (much smaller download)
set -euo pipefail
cd "$(dirname "$0")/.."
TORCH_INDEX="https://download.pytorch.org/whl/cpu"
if [ "${1:-}" != "--cpu" ] && command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  TORCH_INDEX="https://download.pytorch.org/whl/cu128"
fi
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install torch==2.11.0 torchvision==0.26.0 --index-url "$TORCH_INDEX"
.venv/bin/pip install -r requirements-dev.txt
# ultralytics pulls in opencv-python, whose bundled Qt clashes with PySide6.
.venv/bin/pip uninstall -y opencv-python >/dev/null 2>&1 || true
.venv/bin/pip install --force-reinstall --no-deps opencv-python-headless==5.0.0.93
.venv/bin/python scripts/download_model.py
echo "Setup complete. Start the app with ./start.sh"
