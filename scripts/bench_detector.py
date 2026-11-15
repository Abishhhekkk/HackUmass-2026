#!/usr/bin/env python3
"""Time the person detector per frame: whole-frame vs tiled, on THIS device (run on the Jetson).

    python scripts/bench_detector.py --weights models/yolov8n.engine --device 0

Uses a real frame if --image/--video is given, else Ultralytics' bundled bus.jpg stretched to
1920x1080 (speed depends on the frame size and tile count, not on the content). Prints ms per
frame and frames per second for each mode, so you can choose ``detector.tile`` in the config.
Detection only (no tracking or geolocation, which are negligible next to it).
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
from rescue_edge.tiling import predict_tiled, tile_grid  # noqa: E402


def load_frame(args):
    import cv2

    if args.video:
        cap = cv2.VideoCapture(args.video)
        ok, frame = cap.read()
        cap.release()
        if not ok:
            raise SystemExit(f"cannot read a frame from {args.video}")
        return frame
    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            raise SystemExit(f"cannot read {args.image}")
        return frame
    import ultralytics

    bus = cv2.imread(str(Path(ultralytics.__file__).parent / "assets" / "bus.jpg"))
    return cv2.resize(bus, (args.width, args.height))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", required=True, help=".engine (Jetson) or .pt")
    ap.add_argument("--device", default="0")
    ap.add_argument("--imgsz", type=int, default=640, help="must equal the engine's export size")
    ap.add_argument("--tiles", type=int, nargs="+", default=[0, 640, 480, 320],
                    help="0 = whole frame, else tile size in frame pixels")
    ap.add_argument("--video", help="take the first frame of this video (e.g. your footage)")
    ap.add_argument("--image")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--iters", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=3)
    args = ap.parse_args()

    from ultralytics import YOLO

    frame = load_frame(args)
    h, w = frame.shape[:2]
    model = YOLO(args.weights, task="detect")
    device = int(args.device) if args.device.isdigit() else args.device
    half = not args.weights.endswith(".engine") and device != "cpu"

    def predict(img):
        r = model.predict(img, imgsz=args.imgsz, conf=0.1, classes=[0], device=device,
                          half=half, verbose=False)[0]
        return r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()

    print(f"{args.weights} on {args.device}, frame {w}x{h}, model input {args.imgsz}")
    print("| mode | model runs/frame | ms/frame (median) | frames/s | people found |")
    print("|---|---|---|---|---|")
    for tile in args.tiles:
        if tile:
            runs = len(tile_grid(w, h, tile, 0.2)) + 1

            def step():
                return predict_tiled(predict, frame, tile, 0.2, full_frame=True)
        else:
            runs = 1

            def step():
                return predict(frame)
        for _ in range(args.warmup):
            step()
        times = []
        for _ in range(args.iters):
            t0 = time.perf_counter()
            boxes, _ = step()
            times.append(time.perf_counter() - t0)
        ms = 1000 * statistics.median(times)
        name = f"tiles {tile} px" if tile else "whole frame"
        print(f"| {name} | {runs} | {ms:.0f} | {1000 / ms:.1f} | {len(boxes)} |")
    print("Pick the smallest tile whose frames/s is enough: with the adaptive scheduler the\n"
          "detector runs on every 4th frame while searching and every frame when people are in view.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
