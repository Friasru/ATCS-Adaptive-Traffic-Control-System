# Counting accuracy: webcam false crossings and the travel rule

Investigation date: 2026-10-23. Machine: Windows 11, RTX 5060 Laptop, built-in "Integrated Camera";
replays on Ubuntu 24.04 (WSL2) with the same model (`yolo26n.pt`, ByteTrack).

This document keeps **counting accuracy** (does the count match what really happened?) separate from
**automated test success** (does the code do what the tests say?). Passing tests do not prove accurate counts.

## 1. The reported problem

The first physical-webcam session counted **two person crossings 94 ms apart, in opposite directions, by the
same track** (#7). Nobody had crossed the line. The person was lying at the bottom of the frame, on the
counting line.

## 2. Root cause

Before this change, a side of the line counted as "stable" once the anchor (the box centre) was more than a
**fixed** dead zone of 1 % of the frame diagonal away from the line, about 15 px at 1280×720. The crossing was
committed on the first frame the anchor appeared on the other side, and each track could count once per direction.

The centre of a large box moves by tens of pixels while the object stays still:
* a person seated close to a webcam (box about 757×427 px) shifts posture,
* a partly occluded person's box switches between head-only and full-body,
* a box straddling the line changes shape.

Each such movement over 15 px either way produced one `a_to_b` and one `b_to_a`.

## 3. Method

1. **Recorded detection streams.** The real detector and tracker were run once over each clip, and the exact
   detections the counter receives were saved for every frame. Counting variants were then replayed against
   identical inputs, and the replay reproduced the app's own results exactly.
2. **Data sets:**
   * `person-bicycle-car-detection.mp4` and `street_slovenska.mp4`: hand-labelled crossings in
     `tests/data/ground_truth.json` (13 labelled crossings in total).
   * **Real webcam clip, 90 s** (raw, without annotations): a person sits straddling the vertical centre line,
     stands, walks out to the right, comes back and sits down. Labelled by inspecting frames: **2 real crossings**
     of the vertical centre line (walking out and walking back in). Sitting on the line and sitting down are
     not crossings. The horizontal line has **0** crossings. The detections (boxes only, no imagery) are in
     `tests/data/webcam_straddle_detections.json`.
   * `street_cebu.mp4` and `people-detection.mp4`: **unlabelled**. Used to see which counts change, and every
     removed event was then inspected by eye.
3. Every rejected or removed event was checked against the video frames, not only against the numbers.

## 4. What was tried

| Variant | Labelled clips TP/FP/FN | Webcam (true 2) | Cebu, horizontal line | Verdict |
|---|---|---|---|---|
| Old: fixed 1 % dead zone | 13 / 5 / 0 | **4** counted, of which 2 false | 39 | the bug |
| Dead zone of 0.5 × object size | 11 / 3 / **2** | 0 | – | rejected: missed a car driving away and a bicycle that appeared close to the line |
| Dead zone of 0.3 × object size | 13 / 5 / 0 | 0 | **10** | rejected: cars driving *towards* the camera grow faster than they cross (box height 81 → 364 px), so 29 real crossings were lost |
| Per-track jitter estimate | – | – | – | rejected after measuring: the seated person's frame-to-frame jitter is only about 1 px. Their centre *drifts* slowly, it does not jitter |
| Track-ID stitching (a new ID continues a vanished one) | 13 / **6** / 0 | – | – | removed: no benefit on real data, and one extra false positive. It fired about 60 times in a busy scene, so it risks merging different people |
| **Travel rule (shipped)** | **13 / 5 / 0** | **2** (both real) | 35 | adopted |

### The travel rule

A side change counts only once the anchor has travelled at least **0.4 × the object's size** from its farthest
point on the old side. "Size" is the box extent across the line, the **smaller** of its value at that farthest
point and its current value, capped at 20 % of the frame diagonal. Until then the old side is kept, so a slow
crossing is not lost; it is counted when the travel is complete. The fixed 1 % dead zone stays as before.

Why the *smaller* size: approaching objects are small at their far point, and receding objects are small now,
so both stay countable. The rule is strict only for objects that are large the whole time, which are the ones
whose centre drifts.

Parameter sensitivity (all data sets above):

| `min_travel_frac` | 0.0 | 0.2 | 0.3 | **0.4** | 0.5 | 0.6 |
|---|---|---|---|---|---|---|
| Labelled TP/FP/FN | 13/5/0 | 13/5/0 | 13/5/0 | **13/5/0** | 13/5/0 | 13/5/0 |
| Webcam vertical (true 2) | 4 | 3 | 3 | **2** | 2 | 2 |
| Cebu horizontal | 39 | 36 | 35 | **35** | 30 | 28 |

The cap was 10 % at first. A live run then counted a seated person who leaned sideways close to the camera,
because the cap took effect for any box wider than 367 px. At 20 % it applies only to boxes larger than half
the frame diagonal. Caps of 10–30 % give identical counts on every recorded data set.

## 5. Results

### Counting accuracy (real data)

| Data | Old | New | Notes |
|---|---|---|---|
| Labelled clips (13 true crossings) | TP 13, FP 5, FN 0 | **TP 13, FP 5, FN 0** | No legitimate crossing lost. All 5 FPs are a parked bicycle rack that a **panning camera** carries across the line, which is out of scope (the camera must be fixed). |
| Real webcam clip, vertical line (2 true) | 4 (2 false) | **2 (0 false)** | |
| Real webcam clip, horizontal line (0 true) | 0 | **0** | |
| Cebu, horizontal (unlabelled) | 39 | 35 | The 4 removed events: 3 clearly false (a track ID that jumped from a rider to a truck; a jeepney moving sideways while straddling the line, twice) and 1 ambiguous (a 6-frame scooter track). |
| Cebu, vertical / people-detection | 64 / 7 + 3 | 64 / 7 + 3 | unchanged |

### Live physical-webcam runs (new counter, `scripts/camera_check.py`)

| Run | What was in view | Crossings | Assessment |
|---|---|---|---|
| 60 s, cap 10 % | seated person, leaning | 1 | **false**. This led to the 20 % cap. It could not be replayed (that run logged no detections). |
| 90 s, cap 10 %, `--event-log all` | seated person | 0 | correct (0 under the old rule as well) |
| 45 s each, CUDA and CPU `.exe` | seated person | 0 / 0 | correct |

**Not yet validated:** a live session in which someone deliberately walks across the line several times.
That requires a person in front of the camera, so the walking-across behaviour has been checked only on the
recorded webcam clip, not live. The 20 % cap is justified by analysis and the sweep above, but it has not been
checked against a live repeat of the lean.

### Automated tests

`tests/test_counter.py` has 16 new test functions (18 cases; 36 → 54 counter tests) for: drift while sitting on the line, box-shape jumps,
sitting down close to the camera, standing near the line, small-object jitter, slow and noisy crossings,
large objects crossing slowly, crossing and coming back, an approaching car (growing box), a receding car,
an object appearing close to the line, very large boxes, the feet anchor, an ID switch during a crossing
(a documented miss), and the recorded webcam detections. The four drift and jitter tests **fail** under the
old rule; the legitimate-crossing tests pass under both, so they guard against suppressing real crossings.

## 6. Remaining limitations

* **Moving camera:** a camera that pans makes stationary objects cross. Counting assumes a fixed camera.
* **ID switches:** a new ID that appears inside the dead zone has no known starting side, so a crossing
  completed under it is missed (`test_track_id_switch_mid_crossing_is_missed`). A new ID created right
  after a crossing can count it again (`test_track_id_switch_overcounts_and_this_is_visible`).
  *Unique observed* counts tracker IDs: the 90 s webcam clip shows 3 "unique" people for one person.
* **Objects that only partly cross:** a person walking back in and sitting down just past the line counts,
  because their centre travelled from fully on one side. Under a whole-body interpretation that would not be a crossing.
* **Small evidence base:** two labelled clips and one labelled webcam clip. The cut-off between 0.3 and 0.4
  is set by single tracks. Re-run `scripts/evaluate_counts.py`, and replays of `camera_check.py --event-log all`
  sessions, when more labelled footage is available.

## 7. Reproduce

```bash
.venv/bin/python scripts/evaluate_counts.py                               # labelled clips, real engine
.venv/bin/python -m pytest tests/test_counter.py -v                       # includes the recorded webcam detections
.venv-win\Scripts\python.exe scripts\camera_check.py --seconds 90 --event-log all   # live, keeps every detection
```
`min_travel_frac` is in **Settings → Crossing travel (fraction of object size)**, and in `data/settings.json`.
