# Performance and accuracy measurements

All numbers below were **measured** on 2026-10-23 with the code in this repository. Raw JSON is written to
`data/benchmarks/` (git-ignored) by the scripts named in each section.

## Test machine

| | |
|---|---|
| CPU | Intel Core Ultra 9 275HX, **but the WSL2 VM exposes only 2 logical CPUs** (PyTorch uses 2 threads) |
| RAM | 3.8 GiB visible to the VM |
| GPU | NVIDIA GeForce RTX 5060 Laptop GPU, 8 GB (driver 616.56, CUDA 12.8 runtime) |
| OS | Ubuntu 24.04 on WSL2 (kernel 6.6.87.2), WSLg display |
| Software | Python 3.12.3, PyTorch 2.11.0+cu128, Ultralytics 8.4.184, OpenCV 5.0.0, PySide6 6.12.0 |

A normal desktop install with access to all CPU cores should be faster on CPU than shown here.

## 1. Inference + tracking latency (`scripts/benchmark.py micro`)

Model: YOLO26n with ByteTrack. 120 consecutive frames of `street_cebu.mp4` (night street traffic) were resized
to the input resolution. The table shows the mean per-frame `model.track` time and the throughput, including
counting and annotation.

| Input | imgsz | CPU ms (p95) | CPU FPS | GPU ms (p95) | GPU FPS |
|---|---|---|---|---|---|
| 640×480 | 320 | 14.0 (14.5) | 68.3 | 10.5 (11.8) | 93.2 |
| 640×480 | 640 | 48.4 (52.3) | 20.5 | 9.1 (11.2) | 107.2 |
| 640×480 | 960 | 113.6 (123.5) | 8.8 | 11.3 (12.5) | 86.1 |
| 1280×720 | 320 | 12.2 (12.8) | 78.9 | 11.3 (11.5) | 85.2 |
| 1280×720 | 640 | 37.2 (38.1) | 26.5 | 10.7 (11.4) | 89.4 |
| 1280×720 | 960 | 78.8 (80.2) | 12.6 | 12.3 (12.7) | 78.4 |

* 16:9 input is cheaper than 4:3 at the same `imgsz`, because Ultralytics letterboxes to a rectangle (640×384 vs 640×480).
* Annotation (drawing) costs 0.3–0.7 ms per frame.
* On the GPU the cost is dominated by fixed per-call overhead (about 10 ms), so 960 px costs almost the same as 640 px.
* YOLO11n was also measured. It is similar or slightly slower on CPU (e.g. 1280×720 at 960 px: 97.8 ms vs 87.7 ms),
  and it was less accurate in the evaluation below, so YOLO26n is the default.

## 2. Live pipeline (`scripts/benchmark.py pipeline`)

This runs the full engine: capture thread → detection + tracking → counting → annotation → recording → SQLite.
The source is `street_cebu.mp4` (1280×720) paced at its native 15 FPS, with recording on and default settings.
Values are averaged over 30 s after a 3 s warm-up.

| Device (auto size) | Capture FPS | Inference FPS | Recording FPS | Latency | Process CPU* | RSS peak | GPU mem peak** | Rec. dropped / duplicated |
|---|---|---|---|---|---|---|---|---|
| CPU (640 px) | 15.0 | 15.0 | 15.0 | 38.9 ms | 69% | 878 MB | n/a | 0 / 0 |
| CUDA (960 px) | 15.0 | 15.0 | 15.0 | 15.3 ms | 36% | 2102 MB | 54 MB | 0 / 0 |

\* psutil process CPU, where 100% = one fully busy core (the VM has 2).
\*\* `torch.cuda.max_memory_allocated`. GPU utilisation % was not sampled.

The pipeline kept up with the source on both devices, with no dropped frames. Most of the extra RSS with CUDA is the CUDA
runtime libraries. CPU mode uses less than half the memory.

### Other measurements
| What | Result | How |
|---|---|---|
| Idle app (window open, nothing running) | 0.4% CPU, 115 MB RSS, 6 threads | psutil on `./start.sh` process, 8 s sample |
| Model load + first inference | ~5 s on CUDA (packaged build log: start 03:47:13 → loaded 03:47:18) | `data/logs/atcs_monitor.log` |
| UI responsiveness during inference | max Qt event-loop gap < 250 ms (assertion) while detecting + recording | `tests/test_gui.py::test_full_gui_session_with_recording_history_and_export` |
| Recording size (mp4v) | 1280×720 @ 15 FPS: 13.5 MB per 14.9 s (≈54 MB/min); 768×432 @ 12.5 FPS: 1.78 MB per 30.2 s (≈3.5 MB/min) | validated recordings |

### Defaults chosen from these numbers
* **Inference size Auto = 640 px on CPU, 960 px on GPU.** 640 px stays above 20 FPS on 2 vCPUs. 960 px
  is close to free on the GPU and fixed every small-object miss in the evaluation below.
* **Max processed FPS (camera) = 15.** This leaves CPU headroom and keeps recordings at a predictable frame rate.

## 3. Accuracy against hand-labelled ground truth

`scripts/evaluate_counts.py` runs the real engine and compares its line-crossing totals with labels in
`tests/data/ground_truth.json`. The labels were made by inspecting raw frames (0.5 s contact sheets),
**without looking at model output**. Scoring is per line, direction and class: TP = min(expected, counted),
FP = surplus, FN = shortfall.

### Clip A: `person-bicycle-car-detection.mp4` (Intel, CC BY 4.0)
High oblique view of an empty parking lot, 768×432 at 12 FPS, 647 frames. Lines: `h50` (horizontal, y=0.5) and `v80` (vertical, x=0.8).

| Line | Dir | Class | Expected | GPU auto (960) | CPU auto (640) |
|---|---|---|---|---|---|
| h50 | A→B | person | 1 | 1 | 1 |
| h50 | A→B | car | 2 | 2 | 1 |
| v80 | A→B | person (incl. riders) | 3 | 3 | 1 |
| v80 | A→B | bicycle | 2 | 2 | 0 |
| | | **precision / recall** | | **1.00 / 1.00** | **1.00 / 0.375** |

Why the CPU default (640 px) misses objects, from per-track diagnostics:
* The cyclists are about 20 px tall at the far edge of the frame. At 640 px they are detected only sporadically, and their
  tracks fragment right at the line (a new ID starts on the far side).
* The white car is first detected at y=0.48, already at the line, while it is only partly visible.

### Clip B: `street_slovenska.mp4` (Wikimedia Commons, CC BY 3.0)
Handheld street-level camera, 1280×720 at 15 FPS, 814 frames. Line `v50` (vertical, x=0.5). Pedestrians were not labelled (too many tiny distant people).

| Dir | Class | Expected | GPU auto (960) | CPU auto (640) |
|---|---|---|---|---|
| A→B | bus | 2 | 2 | 2 |
| A→B | bicycle | 2 | 4 | 3 |
| B→A | bicycle | 1 | 4 | 5 |
| | **precision / recall** | | **0.50 / 1.00** | **0.50 / 1.00** |

All 5 real crossings were counted on both devices. **All false positives come from parked bicycles in a rack near the
line.** The camera is handheld and pans slowly, so the parked bikes drift ±5–10% of the frame width
across x=0.5 in image coordinates (tracks 52, 76, 113, 127, 167, 212 and 288 in the diagnostic dump). A fixed traffic
camera does not have this problem. The fix is installation guidance (fixed mount, and don't place the line across parked
vehicles), not a change to the counting rule.

### Overall
| Setting | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|
| GPU, auto (960 px) | 13 | 5 | 0 | 0.72 | 1.00 |
| CPU, auto (640 px) | 8 | 5 | 5 | 0.62 | 0.62 |
| GPU, YOLO11n @ 960 px | 12 | 11 | 1 | 0.52 | 0.92 |

The sample is small (13 labelled crossings), so these numbers describe these clips only. They are **not** a general
accuracy figure for the app.

### Top-down footage
On `car-detection.mp4` (camera pointing straight down), YOLO26n detected a car in only 23 of 377 frames at conf 0.25,
even though cars are visible in far more frames. COCO models are a poor fit for overhead views.
