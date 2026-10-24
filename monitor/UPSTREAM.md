# ATCS Monitor (imported source)

This folder is the ATCS Monitor desktop application, imported unchanged from its own repository so the ATCS web app can use its detection engine in later phases. The desktop app still runs on its own from this folder.

## Source revision

| | |
|---|---|
| Repository | https://github.com/Friasru/ATCS-Monitor |
| Commit | `c002df4da800248a87e63d9ec86c5e7443f57386` (2026-10-23, "Windows installer, installed-mode data folder, safe shutdown, data-preserving builds") |
| Method | `git archive` of that commit, so only tracked source files were copied |

Every imported file is byte-identical to that commit. `README.md` and `docs/` are the upstream documents; their paths are relative to this folder.

### Added in this repository

These files are not in the upstream repository; they connect the engine to the ATCS web backend and do not change any imported file:

- `web_service.py` - local HTTP service (127.0.0.1) around `MonitoringEngine`
- `tests/test_web_service.py`
- `docs/web_service.md` - setup, security model and API
- `docs/desktop_telemetry.md` - documents a desktop sends to the ATCS backend (the backend side is in `backend/`; no monitor code changed)

### Not imported

Packaging and installer tooling stays in the ATCS-Monitor repository, which is where the Windows installers are built:

- `packaging/` (PyInstaller spec, Inno Setup installer script)
- `scripts/build.sh`, `scripts/build.ps1`, `scripts/build_installer.ps1`, `scripts/preserve_runtime_data.py`, `scripts/make_icon.py`
- `tests/test_build_preservation.py` (tests those build scripts)

The README's "Packaging" section and the build steps in `docs/windows.md` refer to these files.

Runtime data was never in Git and is not here either: virtual environments, model weights (`models/*.pt`), the SQLite database, settings, logs, recordings, exports, downloaded test videos and webcam clips. `.gitignore` here and in the repository root keep them out.

## Environment

The monitor has its own virtual environment, `monitor/.venv`, separate from the Flask backend's. It needs PyTorch, Ultralytics, OpenCV and PySide6 (about 7.4 GB with the CUDA build of PyTorch); the backend needs none of them.

Do not add `monitor/` to the backend's Python path: both projects have a top-level package named `app`.

## Setup and startup (Linux / WSL2)

    cd monitor
    ./scripts/setup.sh            # GPU build if nvidia-smi works, otherwise CPU-only
    # ./scripts/setup.sh --cpu    # much smaller CPU-only PyTorch
    ./start.sh                    # desktop app
    ./start.sh --video data/test_media/street_cebu.mp4 --start

`setup.sh` also downloads the default weights (`yolo26n.pt`) into `models/`. Test videos are downloaded with `./scripts/fetch_test_media.sh` into `data/test_media/` (sources in `docs/test_media_attribution.md`).

WSL2 has no camera device by default, so use video files there; test real cameras on native Windows.

Headless run on a video file (no window):

    .venv/bin/python scripts/run_video_pipeline.py data/test_media/car-detection.mp4 --line 0.0,0.5,1.0,0.5 --record

## Setup and startup (Windows)

    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
    .\start.cmd

See `docs/windows.md` for camera validation and tests on Windows.

## Data

Everything the app writes stays below this folder unless `ATCS_MONITOR_HOME` points elsewhere: `data/atcs_monitor.sqlite3`, `data/settings.json`, `data/logs/`, `recordings/`, `exports/`, `models/`.

## Tests

    cd monitor
    .venv/bin/python -m pytest

Tests that need the weights or test videos skip themselves when those files are missing. Physical-camera tests only run with `ATCS_TEST_CAMERA=1`.
