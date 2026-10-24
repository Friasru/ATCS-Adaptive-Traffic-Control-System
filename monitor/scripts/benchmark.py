#!/usr/bin/env python3
"""Measure real performance on this machine.

  micro    : per-frame detect+track latency for model x device x imgsz x input resolution
  pipeline : the full engine (capture -> track -> count -> annotate -> record -> db) running
             on a video paced at its native frame rate, sampling the live FPS counters,
             latency, CPU and memory

Results are written as JSON to data/benchmarks/ and printed as a table.
  .venv/bin/python scripts/benchmark.py micro
  .venv/bin/python scripts/benchmark.py pipeline --device cpu --seconds 30
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import paths  # noqa: E402

paths.configure_third_party_dirs()

import cv2  # noqa: E402
import psutil  # noqa: E402

from app.detection.detector import Detector  # noqa: E402

DEFAULT_VIDEO = paths.project_root() / "data" / "test_media" / "street_cebu.mp4"


def hardware() -> dict:
    info = {"python": platform.python_version(), "platform": platform.platform(),
            "cpu": platform.processor() or "", "logical_cpus": os.cpu_count(),
            "ram_gb": round(psutil.virtual_memory().total / 1e9, 1)}
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    info["cpu"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    import torch
    import ultralytics

    info["torch"] = torch.__version__
    info["ultralytics"] = ultralytics.__version__
    info["torch_threads"] = torch.get_num_threads()
    if torch.cuda.is_available():
        info["gpu"] = torch.cuda.get_device_name(0)
    return info


def load_frames(video: Path, n: int, size: tuple[int, int]) -> list:
    cap = cv2.VideoCapture(str(video))
    frames = []
    while len(frames) < n:
        ok, img = cap.read()
        if not ok:
            break
        frames.append(cv2.resize(img, size, interpolation=cv2.INTER_AREA))
    cap.release()
    return frames


def micro(args) -> list[dict]:
    from app.services.annotation import annotate
    from app.tracking.counter import CountingEngine, CountingLine

    proc = psutil.Process()
    out = []
    devices = ["cpu"] + (["gpu"] if args.gpu else [])
    for res in ((640, 480), (1280, 720)):
        frames = load_frames(args.video, args.frames, res)
        for model in args.models:
            for dev in devices:
                det = Detector(model, dev)
                det.load()
                for imgsz in args.imgsz:
                    det.reset_tracker()
                    counter = CountingEngine([CountingLine()])
                    for f in frames[:10]:  # warm-up (also builds the tracker)
                        det.track(f, 0.35, 0.5, imgsz)
                    det.reset_tracker()
                    lat, ann = [], []
                    proc.cpu_percent(None)
                    t_all = time.perf_counter()
                    for i, f in enumerate(frames):
                        t0 = time.perf_counter()
                        dets = det.track(f, 0.35, 0.5, imgsz)
                        t1 = time.perf_counter()
                        upd = counter.update(dets, (res[0], res[1]), float(i))
                        annotate(f, dets, counter.lines.values(), counter.line_totals, upd.visible, upd.crossings,
                                 False, time.time())
                        t2 = time.perf_counter()
                        lat.append(t1 - t0)
                        ann.append(t2 - t1)
                    wall = time.perf_counter() - t_all
                    row = {
                        "input": f"{res[0]}x{res[1]}", "model": model, "device": det.device, "imgsz": imgsz,
                        "frames": len(frames),
                        "track_ms_mean": round(statistics.mean(lat) * 1000, 1),
                        "track_ms_p95": round(sorted(lat)[int(0.95 * len(lat)) - 1] * 1000, 1),
                        "annotate_ms_mean": round(statistics.mean(ann) * 1000, 2),
                        "processed_fps": round(len(frames) / wall, 1),
                        "process_cpu_percent": round(proc.cpu_percent(None), 0),
                        "rss_mb": round(proc.memory_info().rss / 1e6, 0),
                    }
                    print(json.dumps(row))
                    out.append(row)
                del det
    return out


def pipeline(args) -> list[dict]:
    from app.camera.sources import VideoFileSource
    from app.services.monitoring import MonitoringEngine, State
    from app.settings import Settings
    from app.storage.repository import Repository

    tmp = paths.data_dir() / "benchmarks" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    s = Settings(device=args.device, imgsz=args.imgsz[0], model_name=args.models[0])
    repo = Repository(tmp / "bench.sqlite3")
    eng = MonitoringEngine(s, repo, recordings_dir=tmp)
    proc = psutil.Process()
    eng.start(VideoFileSource(args.video, realtime=True), loop=True)
    while eng.state not in (State.DETECTING, State.ERROR):
        time.sleep(0.1)
    eng.start_recording()
    time.sleep(3)  # settle
    proc.cpu_percent(None)
    samples = []
    t_end = time.monotonic() + args.seconds
    while time.monotonic() < t_end:
        time.sleep(1.0)
        sn = eng.latest_snapshot()
        samples.append({"capture": sn.capture_fps, "inference": sn.inference_fps, "recording": sn.recording_fps,
                        "latency_ms": sn.latency_ms, "rss_mb": proc.memory_info().rss / 1e6,
                        "cpu": proc.cpu_percent(None), "dropped_rec": sn.recording_frames_dropped})
    eng.stop(wait=True)
    rec = eng.last_recording
    gpu_mem = None
    if eng.detector and eng.detector.device.startswith("cuda"):
        import torch

        gpu_mem = round(torch.cuda.max_memory_allocated() / 1e6, 0)

    def avg(k):
        return round(statistics.mean(x[k] for x in samples), 1)

    row = {
        "mode": "pipeline", "video": args.video.name, "device": eng.detector.device if eng.detector else args.device,
        "model": s.model_name, "imgsz": s.imgsz, "seconds": args.seconds,
        "capture_fps": avg("capture"), "inference_fps": avg("inference"), "recording_fps": avg("recording"),
        "latency_ms": avg("latency_ms"), "process_cpu_percent": avg("cpu"),
        "rss_mb_peak": round(max(x["rss_mb"] for x in samples), 0), "gpu_mem_mb_peak": gpu_mem,
        "recording_ok": rec.ok if rec else None, "recording_frames": rec.frames_written if rec else None,
        "recording_dropped": rec.frames_dropped if rec else None,
        "recording_duplicated": rec.frames_duplicated if rec else None,
    }
    print(json.dumps(row))
    repo.close()
    for f in tmp.iterdir():  # remove only this script's own temporary files
        f.unlink()
    tmp.rmdir()
    return [row]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["micro", "pipeline"])
    ap.add_argument("--video", type=Path, default=DEFAULT_VIDEO)
    ap.add_argument("--frames", type=int, default=150)
    ap.add_argument("--models", nargs="+", default=["yolo26n.pt", "yolo11n.pt"])
    ap.add_argument("--imgsz", nargs="+", type=int, default=[320, 480, 640])
    ap.add_argument("--no-gpu", dest="gpu", action="store_false")
    ap.add_argument("--device", default="auto", choices=["auto", "cpu", "gpu"])
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()
    if not args.video.is_file():
        print(f"missing {args.video} - run scripts/fetch_test_media.sh", file=sys.stderr)
        return 1
    hw = hardware()
    rows = micro(args) if args.mode == "micro" else pipeline(args)
    out_dir = paths.data_dir() / "benchmarks"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{args.mode}_{stamp}.json"
    path.write_text(json.dumps({"hardware": hw, "results": rows}, indent=2))
    print(f"\nhardware: {json.dumps(hw)}\nsaved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
