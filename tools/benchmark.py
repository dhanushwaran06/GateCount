"""Detection + tracking fps for N streams on this PC, run the same way as the counter
(one grabber thread + one model instance per stream, newest frame only).

Usage:
  python tools/benchmark.py SOURCE [SOURCE ...]                      # 60 s, 640 model
  python tools/benchmark.py --model models/yolo26n_480_openvino_model --seconds 30 a.mp4 b.mp4

SOURCE is a camera index, device path, or video file (files loop and play at real time).
Target: >= 8 fps per stream with 2 streams.
"""

import argparse
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("YOLO_OFFLINE", "True")      # no network checks at runtime
os.environ.setdefault("YOLO_AUTOINSTALL", "False")  # never pip-install at runtime
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil  # noqa: E402  (an ultralytics dependency)
import yaml  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from counter.camera import Camera, quiet_libjpeg_warnings  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Stream(threading.Thread):
    def __init__(self, idx, source, model_path, imgsz, conf, start, stop):
        super().__init__(daemon=True, name=f"bench-{idx}")
        self.source, self.model_path, self.imgsz, self.conf = source, model_path, imgsz, conf
        self.start_evt, self.stop_evt = start, stop
        self.cam = Camera(source, name=f"cam{idx}", loop=True)
        self.ready = threading.Event()
        self.frames = self.persons = 0
        self.infer_s = 0.0
        self.cam_fps = 0.0

    def run(self):
        model = YOLO(self.model_path, task="detect")
        self.cam.start()
        seq = 0
        got = self.cam.read(seq, timeout=10)
        if got:
            self._track(model, got[1])  # warm-up: first call compiles the model
        self.ready.set()
        self.start_evt.wait()
        while not self.stop_evt.is_set():
            got = self.cam.read(seq)
            if not got:
                continue
            seq, frame = got
            t = time.perf_counter()
            res = self._track(model, frame)
            self.infer_s += time.perf_counter() - t
            self.frames += 1
            self.persons += len(res.boxes)
        self.cam_fps = self.cam.fps
        self.cam.stop()
        self.cam.join(3)

    def _track(self, model, frame):
        return model.track(frame, persist=True, classes=[0], conf=self.conf, imgsz=self.imgsz,
                           tracker="bytetrack.yaml", verbose=False)[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+")
    ap.add_argument("--model", default=str(ROOT / "models/yolo26n_640_openvino_model"))
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--conf", type=float, default=0.35)
    args = ap.parse_args()
    restore_stderr = quiet_libjpeg_warnings()

    meta = Path(args.model) / "metadata.yaml"
    imgsz = yaml.safe_load(meta.read_text())["imgsz"] if meta.exists() else 640  # [h, w] of the export
    start, stop = threading.Event(), threading.Event()
    streams = [Stream(i, s, args.model, imgsz, args.conf, start, stop) for i, s in enumerate(args.sources)]
    for s in streams:
        s.start()
    for s in streams:
        s.ready.wait()

    proc = psutil.Process()
    proc.cpu_percent(None)
    psutil.cpu_percent(None)
    print(f"model={Path(args.model).name} imgsz={imgsz} streams={len(streams)} "
          f"cpus={psutil.cpu_count()} seconds={args.seconds:g}")
    start.set()
    t0 = time.monotonic()
    cpu_samples = []
    while (elapsed := time.monotonic() - t0) < args.seconds:
        time.sleep(min(10.0, args.seconds - elapsed))
        cpu_samples.append(psutil.cpu_percent(None))
        line = "  ".join(f"[{i}] {s.frames / (time.monotonic() - t0):4.1f} fps" for i, s in enumerate(streams))
        print(f"{time.monotonic() - t0:5.0f}s  {line}  system CPU {cpu_samples[-1]:.0f}%")
    stop.set()
    elapsed = time.monotonic() - t0
    for s in streams:  # let in-flight inference finish before reading stats / exiting
        s.join(5)
    proc_cpu = proc.cpu_percent(None) / (psutil.cpu_count() or 1)

    print("\nstream  source                                   fps   ms/frame  cam fps  persons/frame")
    ok = True
    for i, s in enumerate(streams):
        fps = s.frames / elapsed
        ok &= fps >= 8
        print(f"{i:>6}  {str(s.source)[-38:]:<38}  {fps:5.1f}  {1000 * s.infer_s / max(1, s.frames):8.1f}"
              f"  {s.cam_fps:7.1f}  {s.persons / max(1, s.frames):13.1f}")
    print(f"\nCPU: this process {proc_cpu:.0f}% of the machine, system avg {sum(cpu_samples) / len(cpu_samples):.0f}%"
          f"   RSS {proc.memory_info().rss / 2**20:.0f} MB")
    print("RESULT:", "PASS (>= 8 fps per stream)" if ok else "FAIL (< 8 fps on some stream)")
    for i, s in enumerate(streams):
        if s.frames / elapsed < 8 and s.cam_fps < 8.5:
            print(f"  stream {i} is limited by the camera ({s.cam_fps:.1f} fps), not the CPU: "
                  "fix exposure/lighting (see README) and re-run")
    restore_stderr()


if __name__ == "__main__":
    main()
