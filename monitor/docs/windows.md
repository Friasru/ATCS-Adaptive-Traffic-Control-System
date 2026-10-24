# ATCS Monitor on Windows

Native Windows 10/11 (x64) support: the app runs on the Windows Python, captures from the webcam
through DirectShow or Media Foundation, and can be packaged as a Windows `.exe`. The Linux/WSL2
setup (`.venv`, `setup.sh`, `start.sh`, `build.sh`) is unchanged. Windows uses its own
environment, `.venv-win`, so one checkout can hold both environments.

> Run the developer steps below in **PowerShell** on Windows (not inside WSL). WSL2 cannot see a
> laptop's built-in webcam without `usbipd`, while native Windows uses it directly.

## 0. Installing ATCS Monitor (no Python, PowerShell or WSL needed)

There are two installers. Install **one**; installing the other later replaces it and keeps your data.

| Installer | Size | For |
|---|---|---|
| `ATCS-Monitor-1.0.0-Setup-CPU.exe` (recommended) | 175 MB (638 MB installed) | any 64-bit Windows 10/11 PC |
| `ATCS-Monitor-1.0.0-Setup-CUDA.exe` (optional) | 1.6 GB (about 3.9 GB installed) | PCs with an NVIDIA GPU and an up-to-date driver: about 2× faster per frame and finds more distant objects (960 px inference) |

1. Double-click the installer and choose **Install for me only** (no administrator rights needed,
   installs to `%LOCALAPPDATA%\Programs\ATCS Monitor`), or *Install for all users* (asks for administrator rights).
2. Keep **Create a desktop shortcut** ticked and finish. Setup offers to start the app.
3. Start it later from the **ATCS Monitor** shortcut on the desktop or in the Start menu.

The installer is not code-signed, so Windows SmartScreen may show "Windows protected your PC" the first
time. Choose **More info → Run anyway**.

**Where your data goes.** The program folder is never written to. Each Windows user's sessions, settings,
logs, recordings and exports are kept in `%LOCALAPPDATA%\ATCS Monitor`:

| Folder | Contents |
|---|---|
| `recordings\` | annotated MP4 recordings (**History → Open Recordings Folder** opens it) |
| `exports\` | CSV/JSON exports |
| `data\` | `atcs_monitor.sqlite3` (session history), `settings.json`, `logs\atcs_monitor.log` |
| `models\` | model weights you download later. The bundled `yolo26n.pt` and `yolo11n.pt` stay in the program folder. |

**Updating or switching** CPU ↔ CUDA: run the other or newer installer. A running ATCS Monitor is asked
to close first. It finishes and saves any recording in progress, exactly as if you had closed the window.

**Uninstalling:** *Settings → Apps → Installed apps → ATCS Monitor → Uninstall*, or *ATCS Monitor* in
*Control Panel → Programs*. This removes the program and its shortcuts. **Your recordings, history, exports and
settings are kept** in `%LOCALAPPDATA%\ATCS Monitor`; the uninstaller tells you where they are. Delete
that folder yourself if you no longer need them. Uninstalling also closes a running ATCS Monitor normally first.

## 1. Requirements

| | |
|---|---|
| OS | Windows 10 or 11, 64-bit |
| Python | 64-bit CPython 3.10 or newer (validated with **3.14.7**). All pinned packages ship native wheels for it. |
| GPU | Optional. With an NVIDIA driver installed (`nvidia-smi` works), setup installs the CUDA 12.8 build of PyTorch. No separate CUDA toolkit is needed because the wheel bundles its runtime. |
| Disk | About 6 GB for `.venv-win` with CUDA PyTorch (about 1 GB with `-Cpu`), plus about 5 GB for a CUDA `.exe` build |
| Git | Optional (only to clone) |

Install Python if `py --list` shows nothing: `winget install Python.Python.3.14`, or use the
installer from python.org (tick *Add python.exe to PATH*, or rely on the `py` launcher).

## 2. Get the code onto a Windows drive

Clone or copy the repository to a normal Windows folder, for example `C:\dev\ATCS-Monitor`.
Do **not** create `.venv-win` inside a `\\wsl$\...` path. It works, but installing and starting
PyTorch over the WSL file share is very slow, and `cmd.exe` cannot use a UNC working directory.

```powershell
git clone https://github.com/Friasru/ATCS-Monitor.git C:\dev\ATCS-Monitor
cd C:\dev\ATCS-Monitor
```

## 3. Install

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          # CUDA PyTorch if an NVIDIA GPU is found
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Cpu     # force the small CPU-only PyTorch
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Python C:\Path\to\python.exe   # pick an interpreter
```

`-ExecutionPolicy Bypass` applies only to that one command and leaves the machine policy unchanged.
`setup.ps1` does the following:
1. creates `.venv-win` (it picks the newest `py -3.x` from 3.14 down to 3.10, then `python`)
2. installs `torch==2.11.0` and `torchvision==0.26.0` from the `cu128` index (or `cpu`)
3. installs `requirements-dev.txt`, which includes the Windows-only `pygrabber` used for camera names
4. replaces `opencv-python` with `opencv-python-headless`, as on Linux
5. downloads `models/yolo26n.pt`
6. prints whether CUDA is available

Confirm the GPU build:

```powershell
.venv-win\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

## 4. Launch

```powershell
.\start.cmd                                                  # or double-click start.cmd in Explorer
.\start.cmd --video data\test_media\street_cebu.mp4 --start  # open a file and start immediately
```

`start.cmd` runs `start.ps1` with the execution policy bypassed for that script only. If your
policy already allows local scripts, you can run `.\start.ps1` directly. The launcher checks
`.venv-win` and the imports, keeps third-party config inside `data\lib_config\`, and starts
`python -m app.main`. Logs are written to `data\logs\atcs_monitor.log`.

## 5. Cameras on Windows

Capture APIs are tried in this order: **DirectShow** (`CAP_DSHOW`), then **Media Foundation** (`CAP_MSMF`).
Each API is used only if the installed OpenCV can load it.

> **Media Foundation is not available with the pinned `opencv-python-headless==5.0.0.93`.** Its
> Windows wheel is built with DirectShow only. MSMF is registered as an optional plugin, but no plugin
> DLL is shipped, so `cv2.videoio_registry.hasBackend(cv2.CAP_MSMF)` is `False`. The app detects this and
> uses DirectShow alone. The MSMF path becomes active automatically with an OpenCV build that includes it.
> The pin was kept so Linux and Windows run the same, tested OpenCV.

* **Find Cameras** reads the DirectShow device names (for example "Integrated Camera") and keeps only
  the devices that actually deliver a frame. A virtual camera such as "OBS Virtual Camera" is
  skipped while OBS is not running. If DirectShow finds no camera, Media Foundation is probed
  instead, when it is available.
* The API that worked during discovery is the one used when you press **Start** and when the app
  reconnects after a disconnect.
* To force an API, set `ATCS_CAMERA_BACKEND` to `dshow`, `msmf` or `any` before launching:
  `$env:ATCS_CAMERA_BACKEND = "msmf"; .\start.cmd`
* When MSMF is available, its hardware transforms are switched off
  (`OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS=0`), because otherwise each open can take several seconds.
* DirectShow reports the camera frame rate as `-1` (unknown). The app treats this as unknown and
  records at *Max processed FPS* (15 by default), or at 15 FPS when that setting is 0 (unlimited).

**Windows camera privacy settings:** *Settings → Privacy & security → Camera* must allow
*Camera access* and *Let desktop apps access your camera*. Otherwise the camera opens but returns no frames.
Only one program can use a webcam at a time, so close Teams, Zoom or the Camera app first.

## 6. Validate the webcam (headless)

`scripts\camera_check.py` checks the whole camera path without the GUI:

```powershell
.venv-win\Scripts\python.exe scripts\camera_check.py --probe-only        # discovery + backend/resolution matrix
.venv-win\Scripts\python.exe scripts\camera_check.py --seconds 30        # + full pipeline with recording
.venv-win\Scripts\python.exe scripts\camera_check.py --index 0 --backend msmf --width 640 --height 480
```

It does the following:
1. lists the cameras
2. opens the camera with each capture API at 640×480, 1280×720 and 1920×1080, and measures the
   resolution and FPS actually delivered
3. runs the real engine (detection, tracking, counting with a vertical line through the image
   centre, annotated recording, SQLite) for `--seconds`. Walk across the frame during this run to
   produce line crossings.
4. checks the results independently: it re-opens and counts the frames of the MP4, compares the
   database totals with the JSON and CSV exports, and compares crossing totals with crossing events

The full report goes to `data\camera_check\camera_check_<time>.json`, and exit code 0 means every
check passed. The session also appears in the app's **History**.

The opt-in pytest hardware tests use the same camera:

```powershell
$env:ATCS_TEST_CAMERA = "1"; .venv-win\Scripts\python.exe -m pytest tests\test_camera_hw.py -v; Remove-Item Env:ATCS_TEST_CAMERA
```

### Manual GUI check
1. Run `.\start.cmd`, then **Find Cameras** and select the camera. **Settings** lets you pick 1280×720.
2. Press **Start**. The status bar should read `Device: CUDA` on an NVIDIA machine, and the boxes should follow you.
3. Press **Counting Lines**, drag the line across the frame and walk across it. The *Line crossings* row should increase.
4. Press **Record**, wait at least 10 s, then **End Recording** and **Stop**. The camera LED must turn off.
5. In **History**, select the session. Recording status should be `ok`. Use **View Recording** and **Export CSV / JSON**.

## 7. Tests

```powershell
.venv-win\Scripts\python.exe -m pytest                       # whole suite
.venv-win\Scripts\python.exe -m pytest -m "not model and not gui"
powershell -ExecutionPolicy Bypass -File scripts\fetch_test_media.ps1   # videos for the media tests
```

`fetch_test_media.ps1` transcodes the two Wikimedia clips with ffmpeg if it is on `PATH`.
Otherwise it uses the project's OpenCV.

## 8. Build the Windows executable

There are two builds. Both have every feature: camera, video files, detection, tracking, counting,
recording, history and export. They differ only in whether inference can use an NVIDIA GPU.

| Build | Command | Size | Inference |
|---|---|---|---|
| **CUDA (accelerated)** | `scripts\build.ps1` (from `.venv-win`) | **3.82 GB** | NVIDIA GPU, or CPU when no GPU is usable |
| **CPU-only (lightweight)** | `scripts\build.ps1 -Venv .venv-win-cpu -Dist dist\windows-cpu` | **0.62 GB** | CPU |

```powershell
# CUDA build (needs .venv-win from setup.ps1 on a machine with an NVIDIA driver)
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
# CPU-only build: separate CPU environment, then build from it (works on any x64 PC)
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -Cpu -Venv .venv-win-cpu
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -Venv .venv-win-cpu -Dist dist\windows-cpu

dist\windows\atcs-monitor\atcs-monitor.exe
dist\windows\atcs-monitor\atcs-monitor.exe --video C:\path\clip.mp4 --start --record --quit-after 20000
dist\windows\atcs-monitor\atcs-monitor.exe --camera 0 --start --record --quit-after 30000
```

`--camera INDEX` selects a discovered camera, and it also works with `start.cmd`. It is a windowed app,
so to see the exit code, use
`(Start-Process dist\windows\atcs-monitor\atcs-monitor.exe -ArgumentList '--camera','0','--start','--quit-after','20000' -Wait -PassThru).ExitCode`.

`build.ps1` first generates the comtypes DirectShow wrappers in the environment, and the build bundles them,
so the frozen app does not generate code at runtime. comtypes still creates an **empty** folder
`%TEMP%\comtypes_cache\atcs-monitor-314` when it is imported. No files are written there.

The build goes to `dist\windows\atcs-monitor\` so that it never overwrites the Linux build in
`dist\atcs-monitor\`. Copy the whole folder, not just the `.exe`. The packaged app keeps `data\`,
`recordings\`, `exports\` and `models\` next to `atcs-monitor.exe`. It is a windowed app with no
console, so check `data\logs\atcs_monitor.log` next to the `.exe` for diagnostics.

### Build the installers

```powershell
# CPU installer (default): needs .venv-win-cpu (setup.ps1 -Cpu -Venv .venv-win-cpu)
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 -Rebuild
# optional CUDA installer: needs .venv-win (setup.ps1 on a machine with an NVIDIA driver)
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1 -Flavor cuda -Rebuild
```

Output: `dist\installer\ATCS-Monitor-<version>-Setup-CPU.exe` / `-CUDA.exe`. The script prints each file's size and SHA-256.

* **Tooling.** [Inno Setup](https://jrsoftware.org/isinfo.php) 6.7.3 is downloaded once from its official
  GitHub release, the SHA-256 is checked, and it is unpacked in portable mode into `build\tools\`.
  Nothing is installed system-wide. The script is `packaging\installer\atcs_monitor.iss`.
* `-Rebuild` runs `build.ps1` first. Without it, the existing `dist\windows*\` folder is packaged. The script
  refuses to build without bundled weights, or from an environment whose PyTorch does not match `-Flavor`.
* **What gets packaged.** The PyInstaller folder, **except** its `data\`, `recordings\` and `exports\` runtime
  folders (a portable build may contain someone's sessions and videos), plus `installed.marker`. The marker
  switches the program to the per-user data folder (`app/paths.py`).
* **Rebuilding keeps portable data.** PyInstaller replaces the whole output folder, so `build.ps1` and
  `build.sh` move a portable build's `data`, `recordings` and `exports` into `dist\...\preserved-<time>\`
  before building, and move them back afterwards (`scripts/preserve_runtime_data.py`). Nothing is overwritten.
  If the build fails, or a file cannot be put back, the data stays in that folder and the script says so.
  `tests/test_build_preservation.py` runs the real build scripts against a stand-in PyInstaller to check this.
* **Icon.** `app\resources\atcs_monitor.ico`, generated by `scripts\make_icon.py`. It is used for the `.exe`,
  the installer, the shortcuts, Apps & features and the app window. The `.exe` also carries Windows version information.
* **Running app during setup.** The app holds the mutex `ATCSMonitor.Running` and closes itself normally when
  the installer sets the event `ATCSMonitor.CloseRequest`. Setup and Uninstall do that, then wait up to 60 s.
  If the app is still running after that, an interactive setup asks you to close it, and a silent one stops
  without changing anything.
* Silent install for IT deployment: `ATCS-Monitor-1.0.0-Setup-CPU.exe /SILENT` (or `/VERYSILENT`).
  Uninstall silently with `"%LOCALAPPDATA%\Programs\ATCS Monitor\unins000.exe" /SILENT`.

### Executable size

The first CUDA build was **4.48 GB**. About 90 % of that is PyTorch, mostly NVIDIA libraries:

| Component (first CUDA build) | MB |
|---|---|
| `torch` total | 4,083 |
| `torch_cuda.dll` | 774 |
| `cublasLt64_12.dll` | 643 |
| `cudnn_engines_precompiled64_9.dll` | 459 |
| `cusparse64_12.dll` | 362 |
| `cufft64_11.dll` | 263 |
| `cudnn_adv64_9.dll` | 257 |
| `torch_cpu.dll` | 254 |
| `cusolver64_11.dll` | 215 |
| `polars` (optional Ultralytics dependency) | 165 |
| `cusolverMg64_11.dll` | 150 |
| OpenCV / PySide6 | 112 / 74 |

What was removed from the CUDA build (−0.66 GB, in `packaging/atcs_monitor.spec`, Windows only):
* `polars` and `matplotlib` (180 MB). They are never imported while the app runs; checked with `sys.modules` after full GPU runs.
* `cusolverMg64_11`, `curand64_10`, `nvperf_host` and `cudnn_adv64_9` (497 MB). No other DLL links against them
  (checked with the PE import tables). With them removed, the GPU pipeline produced **bit-identical detections on
  every frame** of two test clips at the same speed. Repeated full runs are also bit-identical, so the comparison is exact.

What was kept, and why:
* `torch_cuda`, cuBLAS, cuSPARSE, cuFFT, cuSOLVER and nvJitLink are linked directly by PyTorch (2.7 GB).
* The NVRTC libraries: PyTorch fails to start without them (`caffe2_nvrtc.dll` depends on them).
* The cuDNN convolution libraries (`cudnn_ops`, `cudnn_engines_*`, `cudnn_heuristic`): YOLO runs on them.

The trimming was validated on an RTX 5060 (Blackwell). The libraries removed are not specific to a GPU
architecture, but other GPUs were not tested. Further reduction needs a smaller PyTorch, which is what the CPU
build is: `torch_cpu.dll` (254 MB) instead of 3.6 GB.

Performance (`scripts\benchmark.py micro`, Core Ultra 9 275HX + RTX 5060 Laptop, `yolo26n.pt`, detect + track per frame):

| Input | Device | Inference size | Mean ms | p95 ms | FPS |
|---|---|---|---|---|---|
| 1280×720 | CUDA | 960 (GPU default) | 11.8 | 15.8 | 75 |
| 1280×720 | CUDA | 640 | 10.5 | 16.7 | 84 |
| 1280×720 | CPU | 640 (CPU default) | 19.7 | 22.5 | 48 |
| 1280×720 | CPU | 960 | 29.9 | 32.7 | 32 |
| 640×480 | CUDA | 960 | 12.3 | 18.0 | 79 |
| 640×480 | CPU | 640 | 22.6 | 24.4 | 43 |

Both builds are far faster than the camera's frame rate (15 FPS is processed by default). The CPU build uses
roughly 8 cores while running, versus about 2 for the CUDA build, and at its default 640 px it finds fewer
small or distant objects. On `street_slovenska.mp4`: 60 unique people on CPU at 640 px versus 86 on the GPU at 960 px.
Set *Inference size* to 960 in the CPU build if accuracy matters more than CPU load.

## 9. Validation results (2026-10-23)

Machine: Windows 11 (build 26300), Python 3.14.7, RTX 5060 Laptop GPU (driver 616.56), built-in
"Integrated Camera". Everything below ran natively on Windows, not in WSL.

| Check | Result |
|---|---|
| `setup.ps1` | `.venv-win` created. `torch 2.11.0+cu128`, CUDA available on the RTX 5060 |
| Camera discovery | "Integrated Camera" (#0) via DirectShow in 1.5–2.1 s. "OBS Virtual Camera" correctly excluded (no frames) |
| DirectShow probe | 640×480, 1280×720 and 1920×1080 all delivered at the requested size, at about 26–27 FPS (open about 1.7 s) |
| Media Foundation | Not available in the pinned OpenCV wheel (see section 5) |
| `camera_check.py --seconds 30` (1280×720, CUDA) | **pass**. All 13 checks passed: capture about 30 FPS, about 10.5 FPS processed, 11–20 ms latency. Person detected and tracked. 2 line crossings saved and exported. Recording 1280×720 / 15 FPS, 429 frames, 28.6 s, all decodable. DB, JSON and CSV consistent |
| `pytest` (whole suite) | **180 passed**, 5 skipped (the opt-in camera tests). With `ATCS_TEST_CAMERA=1`: **185 passed** (after the counting changes; it was 165 before them) |
| `ATCS_TEST_CAMERA=1 pytest tests\test_camera_hw.py` | **5 passed** on the Integrated Camera |
| `start.cmd --video … --start --record --quit-after 20000` | GUI launched, camera listed, CUDA, recording saved, exit 0 |
| `build.ps1` | First build 4.48 GB (CUDA). Trimmed CUDA build **3.82 GB**, CPU-only build **0.62 GB** |
| Trimmed CUDA `.exe`, full `street_slovenska.mp4` | counts identical to the development environment (unique 86/21/43/2/1, 2 bus crossings), CUDA, exit 0 |
| CPU `.exe`, full `street_slovenska.mp4` | counts identical to the CPU environment (unique 60/11/33/6/1, 1 bus crossing), CPU, exit 0 |
| Both `.exe`, `--camera 0 --start --record` (45 s) | person detected and tracked, 0 false crossings, recording fully decodable (534 / 590 frames), DB integrity ok, exports consistent, exit 0 |
| `.exe --camera 0 --start --record` (run from `%TEMP%`) | Camera opened via DirectShow, session `completed`, recording 214 frames / 14.3 s, all decodable. DB integrity ok, exports consistent, exit 0. No person was in view, so this run had 0 detections |
| `.exe --video street_slovenska.mp4 --start --record` | 118 unique objects detected on CUDA, recording 537 frames / 35.8 s, all decodable, exit 0 |

### Installer validation (2026-10-23, same machine)

| Check | CPU installer | CUDA installer |
|---|---|---|
| Install (`/SILENT`, per user, no admin) | exit 0, 19 s, 638 MB in `%LOCALAPPDATA%\Programs\ATCS Monitor` | exit 0, 87 s, 3.9 GB |
| Packaged content | `.exe`, `_internal`, both weights, marker, uninstaller. No `data`/`recordings`/`exports`. | same |
| Desktop + Start menu shortcuts "ATCS Monitor", Apps & features entry, icon, version info | yes | yes |
| Double-click shows the install-mode dialog with the app icon ("Install for me only" recommended) | yes (screenshot) | – |
| Launch from the Start menu / Desktop shortcut (no Python, PowerShell or WSL) | window in 2 s, title-bar icon, camera listed, normal close exit 0 | window in 2 s |
| Data location in installed mode | `%LOCALAPPDATA%\ATCS Monitor`. The program folder is byte-identical before and after use (2,981 files). | same |
| Webcam: discover, detect, record, close | DirectShow "Integrated Camera", `yolo26n` on CPU, person detected, recording 409 frames all decodable, DB integrity ok, exports consistent, exit 0 | `cuda:0`, person detected, exit 0 |
| Reinstall keeps history | 1 session before, 2 after a new run | – |
| Switch CUDA ↔ CPU | CPU over CUDA: 0 CUDA DLLs left (640 MB) | CUDA over CPU works, GPU used |
| Upgrade while recording | app closed itself in about 1 s, recording finalized (137 frames, decodable), setup exit 0 | – |
| Uninstall while recording | app closed itself, recording finalized (128 frames, decodable), program, shortcuts and entry removed, user data kept | same |

Not tested: the *Install for all users* (administrator) path, the interactive uninstaller's "your data was kept"
message (the silent uninstaller does not show it), and machines other than this one.

Notes:
* **Crossings:** the two webcam crossings in the first run came from one track straddling the centre line
  (opposite directions, 94 ms apart). This was investigated and fixed; see
  [counting_accuracy.md](counting_accuracy.md). To validate counting live, walk across the line several
  times during `camera_check.py --event-log all` and compare the result with your own count.
* **Processing rate:** with the default *Max processed FPS* of 15 and a 30 FPS camera, about 10 FPS
  is processed, because frames arrive every 33 ms and the throttle accepts one only after a full
  66.7 ms. The same happens on Linux. Inference itself takes about 11 ms.

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| `running scripts is disabled on this system` | Use `start.cmd`, or `powershell -ExecutionPolicy Bypass -File ...` as shown above. |
| "No camera detected" | Check the camera privacy settings, close other apps that use the camera, then try `$env:ATCS_CAMERA_BACKEND="msmf"`. |
| Camera found but "delivers no frames" | Usually the privacy toggle for desktop apps, or another app is holding the camera. |
| Generic names such as "Camera 0" | `pygrabber` is missing or COM failed. Re-run `setup.ps1`. Capture still works. |
| `torch.cuda.is_available()` is False | Update the NVIDIA driver (R570 or newer for CUDA 12.8), then re-run `setup.ps1` (it reinstalls from `cu128`). |
| `DLL load failed` importing torch | Install the latest *Microsoft Visual C++ Redistributable (x64)*. |
| Symlink errors in tests | Not expected: the test fixture falls back to hard links without Developer Mode. |
| Want Media Foundation | Not in the pinned OpenCV wheel (section 5). Use `ATCS_CAMERA_BACKEND=dshow` (the default) |
