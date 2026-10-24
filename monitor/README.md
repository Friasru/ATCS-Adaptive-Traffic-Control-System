# ATCS Monitor

A lightweight desktop program that uses a webcam (or a local video file) to detect, track,
count and record **people, cars, motorcycles, bicycles, buses and trucks**. Everything runs
locally: there is no browser, no server and no cloud upload.

* Real-time YOLO detection (Ultralytics YOLO26n by default) with ByteTrack multi-object tracking
* Three separate metrics: **visible now**, **unique observed**, **line crossings**
* A virtual counting line you drag on the video, with a direction filter (A→B, B→A, both)
* Annotated MP4 recording (boxes, labels, track IDs, counts, timestamp), checked after saving
* SQLite session history with a built-in player, plus CSV/JSON export
* A versioned JSON contract for future integration with the ATCS dashboard ([docs/integration_contract.md](docs/integration_contract.md))

ATCS Monitor is independent of the main ATCS project. It does not control traffic lights and it
does not talk to the ATCS backend.

---

## Contents
1. [Requirements](#requirements)
2. [Installation](#installation)
3. [Starting the app](#starting-the-app)
4. [Using the app](#using-the-app)
5. [How it works](#how-it-works)
6. [Data, recordings and export](#data-recordings-and-export)
7. [Performance](#performance)
8. [Accuracy and known limitations](#accuracy-and-known-limitations)
9. [Testing](#testing)
10. [Packaging](#packaging)
11. [Troubleshooting](#troubleshooting)
12. [Privacy](#privacy)
13. [Licensing](#licensing)
14. [Architecture](#architecture)

## Requirements

| | |
|---|---|
| OS | Linux (tested on Ubuntu 24.04 under WSL2 with WSLg) and **native Windows 10/11 x64** (see [docs/windows.md](docs/windows.md)). macOS camera code exists but is untested. |
| Python | 3.10+ (tested with 3.12.3 on Linux, 3.14.7 on Windows) |
| GPU | Optional. NVIDIA + CUDA 12.8 build of PyTorch is used automatically when available (tested: RTX 5060 Laptop). The app runs on CPU without a GPU. |
| Disk | The `.venv` is 7.4 GB with the CUDA build of PyTorch (much smaller with `--cpu`). Recordings (mp4v) take about 54 MB per minute at 1280×720 / 15 FPS. |
| Display | X11 or Wayland session on Linux; any Windows desktop |

## Installation

Everything installs into a project-local virtual environment (`.venv/`). Nothing is installed system-wide.

```bash
git clone https://github.com/Friasru/ATCS-Monitor.git
cd ATCS-Monitor
./scripts/setup.sh          # GPU build if nvidia-smi works, otherwise CPU-only
# ./scripts/setup.sh --cpu  # force the much smaller CPU-only PyTorch
```

`setup.sh` creates `.venv`, installs `requirements-dev.txt`, replaces `opencv-python` with
`opencv-python-headless` (the non-headless wheel ships Qt plugins that clash with PySide6), and
downloads the default model weights.

**Windows users:** run the installer `ATCS-Monitor-1.0.0-Setup-CPU.exe` (or the optional `-CUDA` one for
NVIDIA GPUs). It creates Desktop and Start menu shortcuts and an uninstaller, needs no administrator rights,
and keeps recordings, history and settings in `%LOCALAPPDATA%\ATCS Monitor`. See
[docs/windows.md](docs/windows.md#0-installing-atcs-monitor-no-python-powershell-or-wsl-needed).

**Windows developers:** run `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1` in PowerShell. It creates a
separate `.venv-win`, so it never touches the Linux `.venv`. Start the app with `.\start.cmd`. Full guide
(webcam validation, tests, `.exe` build): [docs/windows.md](docs/windows.md).

### Model setup

The weights are not stored in Git. Download them once (internet needed only for this step):

```bash
.venv/bin/python scripts/download_model.py              # yolo26n.pt (default)
.venv/bin/python scripts/download_model.py yolo11n.pt   # optional alternative
```

Files go to `models/`. If the file is missing when you press **Start**, the app offers to download it.
After that the app works offline (`YOLO_OFFLINE=true`).

## Starting the app

```bash
./start.sh                                        # from any directory
./start.sh --video data/test_media/street_cebu.mp4 --start   # open a file and start immediately
```

`start.sh` finds the project folder and checks the virtual environment, the dependencies and the display before launching.
It also keeps third-party config files inside the project (`data/lib_config/`).
Logs are written to `data/logs/atcs_monitor.log`.

A packaged build (see [Packaging](#packaging)) is started with `dist/atcs-monitor/atcs-monitor`.

## Using the app

```
+-----------------------------------------------------------------------+
| Source: [Camera: HD Webcam (#0) v] [Find Cameras] [Open Video File...]|
|                                     [ ] Replay video        [Settings]|
+-----------------------------------------------------------------------+
|                     live annotated video                              |
+-----------------------------------------------------------------------+
|                 People  Cars  Motorcycles  Bicycles  Buses  Trucks    |
| Visible now        2      3        0           1        0      0      |
| Unique observed    8     12        3           2        1      4      |
| Line crossings     4      9        1           2        0      0      |
+-----------------------------------------------------------------------+
| Camera: HD Webcam        FPS capture 30.0 | inference 15.0 | 44 ms    |
| Status: Recording ...    Device: CPU | tracks 7                       |
+-----------------------------------------------------------------------+
| [Start] [Stop] [Record] [End Recording]                               |
| [Counting Lines] [x] Line enabled [Both directions v] [Reset Counters]|
|                                                    [History] [Export] |
+-----------------------------------------------------------------------+
```
*(The numbers in this layout sketch are illustrative, not measurements.)*

### Webcam
1. Plug in the camera and press **Find Cameras**. On Linux, cameras are found by probing `/dev/video*`, keeping
   only nodes that actually deliver a frame. On Windows, the DirectShow devices are probed (with their real
   names), and Media Foundation is used as a fallback. `ATCS_CAMERA_BACKEND=dshow|msmf|v4l2|any` forces an API.
   Index 0 is never assumed to exist.
2. Pick it in **Source** and press **Start**. The model loads on first start, which takes a few seconds.
3. Camera resolution is set in **Settings** (640×480, 1280×720 or 1920×1080).

If the camera is unplugged during monitoring, the app tries to reconnect 3 times. It then
finishes the session with status `error`, finalizes any recording, and releases the device.

### Video files (testing without a camera)
**Open Video File...** accepts mp4/avi/mov/mkv/webm/m4v/mpg/wmv when FFmpeg can decode them.
Files are played at their native frame rate and **every frame is processed** (none are dropped).
At the end of the file the session closes. Tick **Replay video** to loop it instead. The source type
is always shown ("Camera" or "Video file"), and sessions record which one was used.

`./scripts/fetch_test_media.sh` downloads the clips used for testing (see [docs/test_media_attribution.md](docs/test_media_attribution.md)).

### Counting line
1. Press **Counting Lines** (it stays pressed while editing). The line appears in magenta with handles **P1** and **P2**.
2. Drag the handles. The position is stored as a fraction of the frame, so it survives window
   resizing and resolution changes. It is saved to `data/settings.json` immediately.
3. Choose **Both directions**, **A → B only** or **B → A only**.
   **Side A is the right-hand side when walking from P1 to P2** (labels A/B are drawn on the video).
   Example: for a horizontal line drawn left→right, A is below the line, so an object moving *up* crosses A→B.
4. **Line enabled** turns counting on or off without losing the line.

Moving the line while monitoring is allowed. Tracks lose their remembered side for that line, so the move itself never creates a count.

### Reset Counters
While monitoring, **Reset Counters** **saves the current session** (counts, events, recording) to
History and continues in a **new session** starting from zero. Counts from different
sessions are never mixed. Stopping, or switching source or camera, also starts a new session the next time.

### Recording
**Record** starts an annotated recording. **End Recording** stops it. A red **● REC** indicator
is shown in the window and burned into the video whenever recording is active. Recording never starts on its own.

### History and export
**History** lists every saved session (date, duration, source, status, unique and crossing totals,
recording file). Selecting a session shows its stored statistics and recording status.
**View Recording** opens a built-in player. **Export CSV / JSON** writes to `exports/`.
**Export** in the main window exports the most recent finished session.

### Settings

| Setting | Default | Notes |
|---|---|---|
| Model | `yolo26n.pt` | any `.pt` in `models/` |
| Device | Auto | Auto uses CUDA if it is available and passes a warm-up inference, otherwise CPU |
| Confidence threshold | 0.35 | a track is shown and counted once a detection reaches it |
| NMS IoU | 0.5 | |
| Inference size | Auto | **960 px on GPU, 640 px on CPU**; see [Performance](#performance) |
| Max processed FPS (camera) | 15 | 0 = unlimited; video files always process every frame |
| Camera resolution | 640×480 | |
| Tracker | ByteTrack | BoT-SORT also available (without ReID) |
| Frames to confirm a track | 3 | before it counts as unique or crosses |
| Crossing anchor point | center | `bottom_center` suits elevated cameras looking at the ground |
| Line dead zone | 0.01 | fraction of the frame diagonal (hysteresis) |
| Crossing travel | 0.4 | how far an object must move across the line, as a fraction of its own size, before it counts; see [docs/counting_accuracy.md](docs/counting_accuracy.md) |
| Event log detail | tracks | `none` (crossings only), `tracks` (+ track confirmed/lost), `all` (+ every detection) |
| Burn count overlay | on | |
| Detected classes | all six | |

Invalid values are rejected before they are applied. A corrupt `settings.json` falls back to the defaults.
Model, device, size, resolution, tracker and confirmation changes apply at the next **Start**.
The other settings apply immediately.

## How it works

**Detection.** Ultralytics YOLO (COCO-trained nano model). The class ids for person, bicycle, car,
motorcycle, bus and truck are **read from the model file**. Startup fails if any class is missing,
so motorcycles can never be counted as bicycles or cars by accident.

**Tracking.** ByteTrack through the official `model.track(persist=True)` API. The model runs at a
confidence floor of 0.1, because ByteTrack's second association stage needs low-score boxes to keep tracks alive
through blur and partial occlusion. The user's threshold is applied per track afterwards. The tracker is reset at
every new session, so IDs restart at 1. Each class votes per frame, and a track's class is its majority vote.

**Three different counts. These are never interchangeable:**

| Metric | Meaning | Behaviour |
|---|---|---|
| Visible now | objects in the current frame | recomputed every frame, so it goes down when objects leave |
| Unique observed | confirmed track IDs in this session | each ID counted once, after 3 frames. **ID switches inflate it.** |
| Line crossings | confirmed tracks that crossed the line | at most once per line per direction per track |

**Crossing rule.** For every track and line, the engine stores the last *stable* side of the line and
the anchor position there. Positions inside the dead zone are ignored. A crossing is counted only when the
track reaches the opposite side, **and** the segment between the two positions intersects the
*finite* line segment (going around the end of the line is not a crossing), **and** the anchor has
travelled at least *Crossing travel* × the object's size from its farthest point on the old side. The
travel rule stops a large object that sits or stands on the line, such as a person in front of a webcam,
from being counted when its box drifts. It was measured against labelled clips and a real webcam
recording without losing a legitimate crossing ([docs/counting_accuracy.md](docs/counting_accuracy.md)).
A crossing by a not-yet-confirmed track is held, then committed once the track is confirmed. These cases are covered by
`tests/test_counter.py`: stationary objects, jitter, oscillation, drift of large boxes, slow, reverse and
simultaneous crossings, objects growing or shrinking with distance, disappear/return, ID changes, line moved
mid-session, stream restart, and recorded webcam detections.

## Data, recordings and export

All data stays inside the project folder (or next to the executable for the packaged build):

| Path | Content |
|---|---|
| `recordings/atcs_YYYYMMDD-HHMMSS_<session8>.mp4` | annotated recordings (never overwritten; a numeric suffix is added if the name exists) |
| `data/atcs_monitor.sqlite3` | sessions, statistics, recordings, crossing events, track events (WAL mode) |
| `data/settings.json` | settings and the counting line |
| `data/logs/` | rotating log files |
| `exports/` | CSV/JSON exports |

**Recording format.** MP4 with the `mp4v` (MPEG-4 Part 2) codec. If that encoder is unavailable, the app falls back
to MJPG in an `.avi`, and the actual format is reported. Frames are written at a constant rate. For video files
this is the file's frame rate; for cameras it is the *Max processed FPS* setting. When processing is slower, the
previous frame is repeated so that the duration matches real time. Duplicated, skipped and dropped frames are
counted and stored. After **End Recording**, the file is reopened and fully decoded: it must exist, be non-empty,
open, and have a valid first and last frame, with a decoded frame count matching the written count. Only then is
it reported as saved. The app checks free disk space at start (≥200 MB) and while recording (stops below 50 MB).
Closing the window while recording finalizes the file first.

To delete recordings, remove the files from `recordings/`. The app never deletes recordings itself.
A session whose file is missing still shows its statistics, and History reports "file missing".

**Interrupted sessions.** If the app is killed, the next start marks unfinished sessions as
`interrupted`, and their unfinished recordings as not finalized.

**Exports.** `session_<id8>.json` uses the session summary format of the
[integration contract](docs/integration_contract.md). `session_<id8>_summary.csv` has the columns
`session_id, source_type, source_id, started_at, ended_at, status, metric, person, car, motorcycle, bicycle, bus, truck, total`
(one row each for `unique` and `crossings`). `session_<id8>_crossings.csv` has
`session_id, timestamp, line_id, track_id, class, direction`. Timestamps are ISO 8601 UTC with an offset. Text
fields that start with `= + - @` are prefixed with `'` to prevent spreadsheet formula injection.

## Performance

These figures were measured on the development machine: an Intel Core Ultra 9 275HX with only **2 logical CPUs and 4 GB RAM visible
to the WSL2 VM**, an RTX 5060 Laptop GPU, PyTorch 2.11 + CUDA 12.8, and Ultralytics 8.4.184.
Full tables and the method are in [docs/performance.md](docs/performance.md). To reproduce them:
`scripts/benchmark.py micro` / `scripts/benchmark.py pipeline`.

| Full pipeline, 720p source at 15 FPS, recording on | Inference FPS | Latency | CPU (1 core = 100%) | RSS |
|---|---|---|---|---|
| CPU, 640 px | 15.0 (source-limited) | 39 ms | 69% | 0.9 GB |
| CUDA, 960 px | 15.0 (source-limited) | 15 ms | 36% | 2.1 GB |

Raw model + tracker throughput on CPU at 1280×720 input: 26.5 FPS at 640 px, 12.6 FPS at 960 px, 78.9 FPS at 320 px.
The idle app uses 0.4% CPU and 115 MB.

## Accuracy and known limitations

The app was evaluated against hand-labelled crossings with `scripts/evaluate_counts.py`. The results
and analysis are in [docs/performance.md](docs/performance.md#accuracy-against-hand-labelled-ground-truth).
Main limitations:

* **The camera must be fixed.** A panning or handheld camera makes parked objects move in image
  coordinates, and they can then cross the line. This was measured on the handheld Slovenska clip.
* **Small or distant objects.** At 640 px (the CPU default), small cyclists and pedestrians are often
  missed. 960 px fixed every miss in the test clip, but costs about 2.3× more compute.
* **Top-down views.** The COCO models were trained mostly on side and oblique views. On a strictly overhead clip
  (`car-detection.mp4`), cars were detected in only about 6% of the frames where they appear.
* **ID switches.** After long occlusions, or when objects cross each other, ByteTrack can assign a new ID. This
  can inflate *unique observed*, and a new ID that crosses the line again can be counted again
  (see `test_track_id_switch_overcounts_and_this_is_visible`). No tracker gives perfect identity.
* **Riders.** A cyclist or motorcyclist is usually detected as a vehicle **and** a person, and both are counted.
* **Night and glare** reduce recall, especially for motorcycles (seen in the Cebu night clip).
* **Physical webcam:** WSL2 exposes no `/dev/video*` devices here. The webcam was validated natively on
  Windows (DirectShow) instead; see [docs/windows.md](docs/windows.md#9-validation-results-2026-10-23).
  Counting a person who deliberately walks across the line has been checked on a recorded webcam clip,
  but not yet live ([docs/counting_accuracy.md](docs/counting_accuracy.md#5-results)).
* **Large objects on the line.** The travel rule suppresses drift, but a person who walks back in and
  sits down just past the line is counted, because their centre crossed after a full walk.
* The macOS camera back-end is untested.

## Testing

```bash
.venv/bin/python -m pytest              # whole suite (about 2 minutes with a GPU)
.venv/bin/python -m pytest -m "not model and not gui"   # fast unit tests only
```

| File | Covers |
|---|---|
| `test_counter.py` | all counting scenarios with deterministic tracks |
| `test_units.py` | geometry, class mapping, settings validation, frame buffers, YOLO result conversion |
| `test_storage_export.py` | SQLite lifecycle, transactions, interrupted sessions, CSV/JSON export, integration contract |
| `test_recording_sources.py` | recorder (CFR timing, codec fallback, disk full, validation), video-file and camera sources |
| `test_engine.py` | engine lifecycle with a scripted detector: inference failures, camera disconnect, rapid start/stop, reset, stop while recording, dropped live frames |
| `test_real_pipeline.py` | **real YOLO** inference/tracking and the full video → detection → tracking → counting → recording → DB → export pipeline |
| `test_gui.py` | pytest-qt: launch, controls, line dragging, settings validation, error dialogs, rapid clicks, UI responsiveness during inference, recording, history, player, export, close while recording |

Tests that need weights or videos are skipped, with the reason, when those files are missing.
GUI tests use Qt's offscreen platform. Physical-camera tests (`tests/test_camera_hw.py`) are opt-in:
set `ATCS_TEST_CAMERA=1` to run them. `scripts/camera_check.py` validates a webcam end to end (discovery,
backend/resolution probe, then detection, tracking, counting, recording, SQLite and exports) and writes a
JSON report to `data/camera_check/`. To verify a camera manually in the GUI: Start → Record → End Recording → Stop, then check History (the recording is decoded and validated
automatically) and confirm the camera LED turns off.

## Packaging

```bash
./scripts/build.sh                  # PyInstaller one-folder build -> dist/atcs-monitor/
dist/atcs-monitor/atcs-monitor      # run it
```

The build bundles Python, PySide6, OpenCV, PyTorch and Ultralytics. Weights are copied into
`dist/atcs-monitor/models/`. The packaged app keeps `data/`, `recordings/` and `exports/` next to the
executable, independent of the current directory. With the CUDA build of PyTorch the folder is
**6.6 GB**; build from a CPU-only environment (`setup.sh --cpu`) for a much smaller package.

Verified for the packaged build: it was launched from `/tmp` with
`--video <file> --start --record --quit-after 20000`. It loaded the bundled weights on CUDA, processed the video,
saved a recording that was validated (224 frames, 14.9 s, first and last frames decodable), stored the session
in its own `data/` folder, and exited with code 0.

**Windows:** `scripts\build.ps1` builds `dist\windows\atcs-monitor\atcs-monitor.exe`: **3.82 GB** with CUDA, or **0.62 GB** for the CPU-only build (`-Venv .venv-win-cpu -Dist dist\windows-cpu`). Both were validated on the built-in webcam and on video files; see [docs/windows.md](docs/windows.md#8-build-the-windows-executable). `scripts\build_installer.ps1 [-Flavor cuda]` wraps a build in an Inno Setup installer in `dist\installer\`.

Extra command-line options: `--video FILE`, `--camera INDEX`, `--start`, `--record` (with `--start`), `--quit-after MS`, `--verbose`.

## Troubleshooting

| Problem | Fix |
|---|---|
| "No camera detected" | Check `ls /dev/video*` and that your user is in the `video` group. On WSL2, USB cameras need `usbipd` passthrough (or run natively on Windows). On Windows, see [docs/windows.md](docs/windows.md#10-troubleshooting). |
| "Camera #N could not be opened" | Another program is using the camera, or permission is missing. |
| "opened but delivers no frames" | Try another resolution in Settings; some cameras only support specific modes. |
| "Model weights not found" | `.venv/bin/python scripts/download_model.py` |
| "Cannot decode video file" | The codec is not supported by the bundled FFmpeg. Convert with `ffmpeg -i in.x -c:v libx264 out.mp4`. |
| Low FPS on CPU | Settings → Inference size 480 or 320, or lower *Max processed FPS*. |
| Qt `xcb` plugin error | Install `libxcb-cursor0`, or run under Wayland (`QT_QPA_PLATFORM=wayland`). |
| GPU not used | Settings → Device shows Auto/GPU. The log says why CUDA was skipped (e.g. warm-up failure). |

## Privacy

* All processing is local. There are no network services, no uploads and no telemetry.
  Network access happens only when you explicitly download model weights. Ultralytics' optional
  usage analytics (`sync`) are switched off in the project-local settings before any model is loaded,
  and the app runs with `YOLO_OFFLINE=true`.
* There is no face recognition, identification, licence-plate reading or biometric classification.
  "person" is a box with a class and a temporary tracker number.
* Recording is always started by the user and is always visible (window indicator plus a burned-in REC mark).
  No microphone is used.
* Recordings and the database stay on local disk until you delete them.

## Licensing

* **Ultralytics** (library and YOLO weights) is licensed under **AGPL-3.0**, with an enterprise licence
  available from Ultralytics. Distributing ATCS Monitor (including the PyInstaller build, which bundles
  the library and the weights) means complying with AGPL-3.0, or obtaining an Ultralytics enterprise licence.
* PySide6 is LGPL-3.0, OpenCV is Apache-2.0, PyTorch is BSD-3-Clause.
* The test videos are third-party (CC BY / CC BY-SA / AGPL) and are not stored in this repository.
  See [docs/test_media_attribution.md](docs/test_media_attribution.md).
* This repository does not yet declare a licence for its own code; that is the owner's decision.

## Architecture

```
app/
  main.py               entry point: logging, crash hook, interrupted-session recovery
  paths.py              project-local paths (also when frozen)
  settings.py           validated settings <-> data/settings.json
  camera/sources.py     CameraSource (V4L2 / DirectShow + Media Foundation / AVFoundation), VideoFileSource, discovery
  camera/manager.py     capture thread, bounded FrameBuffer (latest-only / lossless), RateMeter
  detection/            classes.py (class mapping, colours), detector.py (YOLO + ByteTrack), types.py
  tracking/             geometry.py (segment tests), counter.py (visible/unique/crossings)
  recording/recorder.py CFR VideoRecorder thread + validate_video
  storage/              database.py (schema), repository.py (transactions, EventWriter), export.py
  services/monitoring.py MonitoringEngine: pipeline thread, sessions, recording, events
  services/annotation.py OpenCV drawing shared by preview and recording
  integration/schemas.py versioned JSON contract
  ui/                   main_window, camera_view (aspect-correct view + line editor), settings/history dialogs
```

Threads: **capture → pipeline (inference, counting, annotation) → recorder**, plus a **database writer**
and a **recording finalizer**. The GUI thread only polls the latest snapshot (25 Hz) and receives Qt
signals, so it never waits on a camera, the model, the encoder or the disk. Queues are bounded.
For cameras, stale frames are dropped (and counted); for files, no frame is dropped. Recording frames
are never dropped silently.
