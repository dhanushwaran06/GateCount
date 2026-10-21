"""Book fair people counter — the same program on both gate PCs; only config.yaml differs.

Threads: per camera a grabber + a worker (YOLO track -> line crossing -> totals), one sender, one state
writer. Frames never leave this process and are never written to disk; --debug only shows them on screen.

Usage:
  python -m counter.main --config counter/config.yaml [--debug]
"""

import argparse
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path

os.environ.setdefault("YOLO_OFFLINE", "True")      # no network checks at runtime
os.environ.setdefault("YOLO_AUTOINSTALL", "False")  # never pip-install at runtime

import numpy as np  # noqa: E402
import yaml  # noqa: E402
from ultralytics import YOLO  # noqa: E402

from counter.camera import HEIGHT, WIDTH, Camera, quiet_libjpeg_warnings  # noqa: E402
from counter.crossing import LineCounter  # noqa: E402
from counter.sender import Sender  # noqa: E402
from counter.state import StateWriter, Totals  # noqa: E402

log = logging.getLogger("counter")
ROOT = Path(__file__).resolve().parents[1]


def load_env(path):
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_config(path):
    cfg = yaml.safe_load(Path(path).read_text())
    for key in ("device", "gate", "hub_url", "cameras"):
        if key not in cfg:
            raise SystemExit(f"{path}: missing '{key}'")
    if cfg["gate"] not in ("entry", "exit"):
        raise SystemExit(f"{path}: gate must be 'entry' or 'exit'")
    ids = [c["id"] for c in cfg["cameras"]]
    if len(set(ids)) != len(ids):
        raise SystemExit(f"{path}: camera ids must be unique: {ids}")
    token_env = cfg.get("token_env", "COUNTER_TOKEN")
    cfg["token"] = os.environ.get(token_env, "")
    if not cfg["token"]:
        log.warning("%s is not set: the hub will reject this device's messages", token_env)
    for key, default in (("model", "models/yolo26n_640_openvino_model"), ("state_dir", "state")):
        cfg[key] = ROOT / cfg.get(key, default)
    return cfg


def model_imgsz(model_path):
    """[h, w] the model was exported with (metadata.yaml), else 640."""
    meta = Path(model_path) / "metadata.yaml"
    return yaml.safe_load(meta.read_text())["imgsz"] if meta.exists() else 640


class Worker(threading.Thread):
    """One camera: newest frame -> YOLO + ByteTrack (person only) -> line crossing -> totals."""

    def __init__(self, cam_cfg, cfg, totals, stop, debug):
        super().__init__(daemon=True, name=f"worker-{cam_cfg['id']}")
        self.id, self.cam_cfg, self.cfg, self.totals, self.stop_evt, self.debug = (
            cam_cfg["id"], cam_cfg, cfg, totals, stop, debug)
        controls = {**cfg.get("v4l2", {}), **cam_cfg.get("v4l2", {})}
        self.camera = Camera(cam_cfg["source"], name=f"grab-{self.id}", loop=cam_cfg.get("loop", False),
                             controls=controls)
        self.fps = 0.0                 # processed (inference) fps, reported to the hub
        self.view = None               # (frame, boxes xywh, track ids) for the debug window
        self.flash = (0.0, None)       # (monotonic time, event) of the last crossing, for the debug window
        for key, default in (("line_y", 0.5), ("x_min", 0.0), ("x_max", 1.0), ("direction", "down")):
            cam_cfg.setdefault(key, default)
        self.set_line()

    @property
    def last_frame_at(self):
        return self.camera.last_frame_at

    def set_line(self, **changes):
        """Update line_y / x_min / x_max (fractions) and rebuild the crossing logic."""
        self.cam_cfg.update(changes)
        c = self.cam_cfg
        self.line = LineCounter.from_fractions(c["line_y"], c["x_min"], c["x_max"],
                                               c["direction"], WIDTH, HEIGHT, c.get("margin", 0.04))

    def run(self):
        model = YOLO(str(self.cfg["model"]), task="detect")
        imgsz = model_imgsz(self.cfg["model"])
        conf = self.cam_cfg.get("conf", 0.35)
        device = self.cfg.get("infer_device") or None
        self.camera.start()
        seq, n, t0 = 0, 0, time.monotonic()
        while not self.stop_evt.is_set():
            got = self.camera.read(seq, timeout=1.0)
            if not got:
                if self.camera.ended:
                    log.info("%s: source ended", self.id)
                    break
                continue
            seq, frame = got
            try:
                res = model.track(frame, persist=True, classes=[0], conf=conf, imgsz=imgsz, device=device,
                                  tracker="bytetrack.yaml", verbose=False)[0]
            except Exception:
                log.exception("%s: inference failed", self.id)
                self.stop_evt.wait(1)
                continue
            boxes = res.boxes
            if boxes.id is None:
                xywh, ids = np.empty((0, 4)), []
            else:
                xywh, ids = boxes.xywh.cpu().numpy(), boxes.id.int().cpu().tolist()
            now, line = time.monotonic(), self.line
            for tid, (cx, cy, _, _) in zip(ids, xywh):
                event = line.update(tid, float(cx), float(cy), now)
                if event:
                    self.totals.add(self.id, event)
                    self.flash = (now, event)
                    log.info("%s: %s (track %d)", self.id, "COUNT" if event == "count" else "wrong way", tid)
            n += 1
            if now - t0 >= 2.0:
                self.fps, n, t0 = n / (now - t0), 0, now
            if self.debug:
                self.view = (frame, xywh, ids)
        self.fps = 0.0
        self.camera.stop()
        self.camera.join(3)


# ---------------------------------------------------------------- live window (main thread only)
# One window with every camera side by side, so a gate's monitor can show it full screen (--fullscreen).

WINDOW = "GateCount live"
LINE_KEYS = ("line_y", "x_min", "x_max", "direction")


def save_lines(config_path, workers):
    """Write each camera's line_y / x_min / x_max / direction back into config.yaml, keeping comments and layout."""
    values = {w.id: {k: w.cam_cfg[k] for k in LINE_KEYS} for w in workers}
    out, cam, seen = [], None, set()
    for line in Path(config_path).read_text().splitlines(keepends=True):
        if m := re.match(r"\s*(?:-\s+)?id:\s*['\"]?([\w-]+)", line):
            cam = m[1]
        k = re.match(r"(\s*(?:-\s+)?)(line_y|x_min|x_max|direction)(:\s*)[^#\n]*?(\s*(?:#.*)?\n?)$", line)
        if k and cam in values:
            value = values[cam][k[2]]
            line = f"{k[1]}{k[2]}{k[3]}{value if isinstance(value, str) else f'{value:.3f}'}{k[4]}"
            seen.add((cam, k[2]))
        out.append(line)
    tmp = Path(config_path).with_suffix(".tmp")
    tmp.write_text("".join(out))
    os.replace(tmp, config_path)
    missing = [f"{c}.{k}" for c in values for k in values[c] if (c, k) not in seen]
    log.info("saved line settings to %s%s", config_path, f" (not in file, add them: {missing})" if missing else "")


def on_mouse(event, x, y, _flags, param):
    import cv2
    workers, ui = param
    i = min(max(x // WIDTH, 0), len(workers) - 1)  # which camera tile the mouse is over
    worker, x = workers[i], x - i * WIDTH
    ui["hover"], ui["moved"] = i, time.monotonic()
    if event == cv2.EVENT_LBUTTONDOWN:  # left click: counting line here
        worker.set_line(line_y=round(min(max(y / HEIGHT, 0.05), 0.95), 3))
    elif event == cv2.EVENT_RBUTTONDOWN:  # right click: move the nearest segment edge here
        fx, c = round(min(max(x / WIDTH, 0.0), 1.0), 3), worker.cam_cfg
        edge = "x_min" if abs(fx - c["x_min"]) < abs(fx - c["x_max"]) else "x_max"
        lo, hi = (fx, c["x_max"]) if edge == "x_min" else (c["x_min"], fx)
        if lo < hi:
            worker.set_line(**{edge: fx})


def label(img, text, org, scale=0.75, color=(255, 255, 255)):
    import cv2
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 5)
    cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2)


def draw(worker, counts, highlight=False):
    import cv2
    if worker.view is None:  # never had a frame: say so instead of showing nothing
        img = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        label(img, f"{worker.id}: waiting for camera", (40, HEIGHT // 2 - 20), 1.2, (0, 200, 255))
        label(img, str(worker.cam_cfg["source"])[-60:], (40, HEIGHT // 2 + 30), 0.7)
        return img
    frame, xywh, ids = worker.view
    img, line = frame.copy(), worker.line
    ly, m = line.line_y, int(line.margin)
    age, event = time.monotonic() - worker.flash[0], worker.flash[1]
    color = (0, 220, 0) if age < 0.7 and event == "count" else (0, 0, 255) if age < 0.7 else (0, 220, 255)
    for y in (ly - m, ly + m):  # hysteresis band
        cv2.line(img, (0, y), (WIDTH, y), (0, 120, 160), 1)
    cv2.line(img, (0, ly), (WIDTH, ly), (140, 140, 140), 1)
    cv2.line(img, (line.x_min, ly), (line.x_max, ly), color, 5)
    for x in (line.x_min, line.x_max):
        cv2.line(img, (x, ly - m - 15), (x, ly + m + 15), color, 2)
    ax = line.x_min + 30
    tip = (ax, ly + 60) if line.direction == "down" else (ax, ly - 60)
    cv2.arrowedLine(img, (ax, 2 * ly - tip[1]), tip, color, 3, tipLength=0.3)
    for tid, (cx, cy, bw, bh) in zip(ids, xywh):
        p1, p2 = (int(cx - bw / 2), int(cy - bh / 2)), (int(cx + bw / 2), int(cy + bh / 2))
        cv2.rectangle(img, p1, p2, (255, 160, 0), 2)
        cv2.circle(img, (int(cx), int(cy)), 5, (255, 160, 0), -1)
        cv2.putText(img, str(tid), (p1[0], p1[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 160, 0), 2)
    c, cc = worker.cam_cfg, counts.get(worker.id, {})
    label(img, f"{worker.id}  COUNT {cc.get('count', 0)}  wrong-way {cc.get('wrong_way', 0)}  "
               f"{worker.fps:.1f} fps  ({worker.cfg['gate']}, {line.direction})", (12, 32))
    label(img, f"line_y {c['line_y']:.3f}  segment {c['x_min']:.3f}-{c['x_max']:.3f}", (12, 62))
    label(img, "L-click: line  R-click: segment edge  d: flip direction  s: save  q: quit", (12, HEIGHT - 16), 0.6)
    stale = time.time() - worker.last_frame_at
    if stale > 2:  # a frozen picture must never pass for live
        img = (img * 0.35).astype(np.uint8)
        label(img, "NO SIGNAL", (40, HEIGHT // 2), 2.0, (0, 0, 255))
        label(img, f"{worker.id}: last frame {stale:.0f} s ago - check the USB cable", (40, HEIGHT // 2 + 50), 0.9)
    if highlight:
        cv2.rectangle(img, (0, 0), (WIDTH - 1, HEIGHT - 1), (0, 220, 255), 6)
    return img


def debug_loop(workers, totals, config_path, stop, fullscreen=False):
    import cv2
    # GUI_NORMAL: no Qt toolbar/context menu, so there is no "save image" button (frames never hit disk)
    # and right-click reaches on_mouse.
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL | cv2.WINDOW_KEEPRATIO | cv2.WINDOW_GUI_NORMAL)
    if fullscreen:
        cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
    else:
        cv2.resizeWindow(WINDOW, 960 * len(workers), 540)
    ui = {"hover": 0, "moved": 0.0}
    cv2.setMouseCallback(WINDOW, on_mouse, (workers, ui))
    while not stop.is_set() and any(w.is_alive() for w in workers):
        counts = totals.snapshot()[2]
        pointing = time.monotonic() - ui["moved"] < 5  # outline the tile under the mouse while it is in use
        tiles = [draw(w, counts, pointing and i == ui["hover"]) for i, w in enumerate(workers)]
        cv2.imshow(WINDOW, np.hstack(tiles))
        key = cv2.waitKey(30) & 0xFF
        if key == ord("q"):
            break
        if key == ord("s"):
            save_lines(config_path, workers)
        if key == ord("d"):
            w = workers[ui["hover"]]
            w.set_line(direction="up" if w.cam_cfg["direction"] == "down" else "down")
            log.info("%s: direction is now %s (press s to save)", w.id, w.cam_cfg["direction"])
    cv2.destroyAllWindows()


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "counter/config.yaml"))
    ap.add_argument("--debug", action="store_true", help="show the live camera window on this PC's screen")
    ap.add_argument("--fullscreen", action="store_true", help="with --debug: live window full screen (gate monitor)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per POST is noise; failures are logged by sender

    restore_stderr = quiet_libjpeg_warnings()
    load_env(ROOT / ".env")
    cfg = load_config(args.config)
    if not Path(cfg["model"]).exists():
        raise SystemExit(f"model not found: {cfg['model']} — run: python tools/export_model.py")

    stop, stop_io = threading.Event(), threading.Event()  # workers stop first, then sender/writer flush
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    totals = Totals([c["id"] for c in cfg["cameras"]], cfg["state_dir"])
    workers = [Worker(c, cfg, totals, stop, args.debug) for c in cfg["cameras"]]
    writer = StateWriter(totals, stop_io)
    sender = Sender(cfg, totals, {w.id: w for w in workers}, stop_io)
    for t in (*workers, writer, sender):
        t.start()
    log.info("%s (%s gate) running %d camera(s) -> %s", cfg["device"], cfg["gate"], len(workers), cfg["hub_url"])

    if args.debug:
        debug_loop(workers, totals, args.config, stop, args.fullscreen)
    else:
        while not stop.wait(1.0):
            if not any(w.is_alive() for w in workers):
                log.info("all sources ended")
                break

    stop.set()
    for w in workers:
        w.join(5)
    stop_io.set()
    with totals.changed:
        totals.changed.notify_all()  # wake the sender so it sends the final totals now
    writer.join(5)
    sender.join(10)
    log.info("stopped; totals %s", totals.snapshot()[2])
    restore_stderr()


if __name__ == "__main__":
    main()
