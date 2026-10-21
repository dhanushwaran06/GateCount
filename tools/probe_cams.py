"""List cameras, open each at 1280x720 MJPG, and report the real resolution, format and fps.

Usage:
  python tools/probe_cams.py                  # probe every camera found
  python tools/probe_cams.py --seconds 5 0    # probe specific sources (index, path or video file)

On Linux it prints /dev/v4l/by-id and by-path names: put one of those in counter/config.yaml so the
config does not depend on USB enumeration order. Identical camera models often share a by-id name;
if so, use the by-path name (tied to the physical USB port) and keep each camera in the same port.
"""

import argparse
import glob
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cv2  # noqa: E402

from counter.camera import HEIGHT, WIDTH, open_capture, to_frame  # noqa: E402


def linux_cameras():
    """Map /dev/videoN -> stable names, for capture nodes only (video-index0)."""
    names = {}
    for kind in ("by-id", "by-path"):
        for link in sorted(glob.glob(f"/dev/v4l/{kind}/*-video-index0")):
            names.setdefault(os.path.realpath(link), []).append(link)
    if not names:  # no udev links: fall back to raw nodes
        names = {dev: [] for dev in sorted(glob.glob("/dev/video*"))}
    return names


def probe(source, seconds):
    cap = open_capture(source)
    if not cap.isOpened():
        return "cannot open"
    t_open = time.monotonic()
    ok, frame = cap.read()
    if not ok:
        cap.release()
        return "opened but no frames (metadata node or busy?)"
    first_ms = (time.monotonic() - t_open) * 1000
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode(errors="replace")
    n, t0 = 0, time.monotonic()
    while time.monotonic() - t0 < seconds:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
    fps = n / (time.monotonic() - t0)
    cap.release()
    img = to_frame(frame)
    if img is None:
        return f"{fourcc} {fps:.1f} fps, but the last frame did not decode"
    h, w = img.shape[:2]
    size_flag = "" if (w, h) == (WIDTH, HEIGHT) else f"  <-- expected {WIDTH}x{HEIGHT}"
    fps_flag = "" if fps >= 12 else "  <-- low fps (check lighting/exposure or USB bandwidth)"
    return f"{w}x{h} {fourcc} {fps:.1f} fps, first frame {first_ms:.0f} ms{size_flag}{fps_flag}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", help="index, device path or video file (default: all cameras)")
    ap.add_argument("--seconds", type=float, default=10.0, help="measure fps for this long per camera")
    ap.add_argument("--max-index", type=int, default=6, help="Windows: probe indices 0..N-1")
    args = ap.parse_args()

    if args.sources:
        cams = {s: [] for s in args.sources}
    elif sys.platform.startswith("linux"):
        cams = linux_cameras()
    else:
        cams = {i: [] for i in range(args.max_index)}

    ids = Counter(os.path.basename(n) for links in cams.values() for n in links if "/by-id/" in n)
    print(f"Probing {len(cams)} source(s) for {args.seconds:g} s each at {WIDTH}x{HEIGHT} MJPG\n")
    for dev, links in cams.items():
        print(f"{dev}")
        for link in links:
            print(f"    {link}")
        print(f"    -> {probe(dev, args.seconds)}\n")
    if len(ids) < sum(1 for links in cams.values() if any("/by-id/" in n for n in links)):
        print("WARNING: some cameras share a by-id name; use the by-path names in config.yaml.")


if __name__ == "__main__":
    main()
