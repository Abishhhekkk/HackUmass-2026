#!/usr/bin/env python3
"""Draw two detectors' boxes on the same video frames, side by side, to check counts by eye.

    python scripts/compare_frames.py --video data/sim/crowd.mp4 \
        --a models/yolov8n.engine --b models/person-visdrone.engine --device 0

Uses the same tiling as the edge pipeline (default 640 px tiles + full frame). Boxes:
green = conf >= 0.25 (can start a new tracked person), yellow = 0.10-0.25 (can only extend a
person already being tracked). Writes data/compare/frame_<n>.jpg; open them and count.
Detection only: no tracking, so these are per-frame boxes, not the mission's people count.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
from rescue_edge.tiling import predict_tiled  # noqa: E402

GREEN, YELLOW, WHITE, BLACK = (0, 200, 0), (0, 220, 255), (255, 255, 255), (0, 0, 0)


def make_predict(weights: str, imgsz: int, conf: float, device):
    from ultralytics import YOLO

    model = YOLO(weights, task="detect")
    half = not weights.endswith(".engine") and device != "cpu"

    def predict(img):
        r = model.predict(img, imgsz=imgsz, conf=conf, classes=[0], device=device,
                          half=half, verbose=False)[0]
        return r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()
    return predict


def draw(cv2, frame, boxes, scores, title):
    vis = frame.copy()
    strong = 0
    for (x1, y1, x2, y2), s in zip(boxes.astype(int), scores):
        strong += s >= 0.25
        cv2.rectangle(vis, (x1, y1), (x2, y2), GREEN if s >= 0.25 else YELLOW, 2)
    label = f"{title}: {strong} boxes >= 0.25, {len(boxes) - strong} weak (0.10-0.25)"
    scale = max(0.6, frame.shape[1] / 1600)
    (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
    cv2.rectangle(vis, (0, 0), (tw + 20, th + 24), BLACK, -1)
    cv2.putText(vis, label, (10, th + 12), cv2.FONT_HERSHEY_SIMPLEX, scale, WHITE, 2)
    return vis


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--video", required=True)
    ap.add_argument("--a", required=True, help="first weights (e.g. stock engine)")
    ap.add_argument("--b", required=True, help="second weights (e.g. fine-tuned engine)")
    ap.add_argument("--frames", type=int, default=4, help="evenly spaced frames to compare")
    ap.add_argument("--tile", type=int, default=640, help="0 = whole frame only")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--device", default="0")
    ap.add_argument("--out", default=str(ROOT / "data" / "compare"))
    args = ap.parse_args()

    import cv2
    import numpy as np

    device = int(args.device) if args.device.isdigit() else args.device
    cap = cv2.VideoCapture(args.video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total <= 0:
        raise SystemExit(f"cannot read {args.video}")
    picks = [int(total * (i + 0.5) / args.frames) for i in range(args.frames)]
    models = [(Path(w).stem, make_predict(w, args.imgsz, args.conf, device)) for w in (args.a, args.b)]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"{args.video}: {total} frames; comparing frames {picks} (tile {args.tile or 'none'})")
    print("| frame | " + " | ".join(f"{n} >=0.25 | {n} weak" for n, _ in models) + " |")
    print("|---|" + "---|---|" * len(models))
    for n in picks:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n)
        ok, frame = cap.read()
        if not ok:
            continue
        panels, cells = [], []
        for name, predict in models:
            if args.tile:
                boxes, scores = predict_tiled(predict, frame, args.tile, 0.2, full_frame=True)
            else:
                boxes, scores = predict(frame)
            strong = int((scores >= 0.25).sum())
            cells += [str(strong), str(len(scores) - strong)]
            panels.append(draw(cv2, frame, boxes, scores, name))
        sheet = np.vstack(panels) if frame.shape[1] > frame.shape[0] else np.hstack(panels)
        path = out / f"frame_{n:05d}.jpg"
        cv2.imwrite(str(path), sheet, [cv2.IMWRITE_JPEG_QUALITY, 85])
        print(f"| {n} | " + " | ".join(cells) + " |")
    cap.release()
    print(f"\nwrote {len(picks)} images to {out} (top/left = {models[0][0]}, bottom/right = {models[1][0]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
