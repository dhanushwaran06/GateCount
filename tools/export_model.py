"""Download yolo26n.pt and export it to OpenVINO for CPU inference. Run once per PC (needs internet).

Frames are always 1280x720 (16:9), so the model is exported with a 16:9 input (640x384 for imgsz 640)
instead of a letterboxed 640x640 square: same detail, ~35% less compute.

Usage:
  python tools/export_model.py               # -> models/yolo26n_640_openvino_model/  (input 640x384)
  python tools/export_model.py --imgsz 480   # -> models/yolo26n_480_openvino_model/  (input 480x288)
"""

import argparse
import math
import shutil
from pathlib import Path

from ultralytics import YOLO

MODELS = Path(__file__).resolve().parents[1] / "models"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--imgsz", type=int, default=640, help="long side (width) of the model input")
    args = ap.parse_args()

    height = math.ceil(args.imgsz * 9 / 16 / 32) * 32  # 16:9, rounded up to the model stride
    MODELS.mkdir(exist_ok=True)
    model = YOLO(str(MODELS / "yolo26n.pt"))  # downloads the weights here if missing
    exported = Path(model.export(format="openvino", imgsz=(height, args.imgsz), half=False, dynamic=False))
    target = MODELS / f"yolo26n_{args.imgsz}_openvino_model"
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(exported), target)
    print(f"\nOpenVINO model ready: {target} (input {args.imgsz}x{height})")


if __name__ == "__main__":
    main()
