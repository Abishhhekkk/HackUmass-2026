#!/usr/bin/env python3
"""Run the person detector + tracker on an .mp4 and write a copy with the results drawn on it.

    python scripts/annotate_video.py data/footage/clip.mp4 --weights models/person-visdrone.engine

Same detector, tiling and tracker as the edge pipeline (rescue_edge.detector.build_detector), so
what you see is what the pipeline sees. Writes <clip>_annotated.mp4 next to the input (or --out):

* boxes: green = confidence >= 0.25, yellow = weaker (0.10-0.25); label = track ID
* top bar: frame, people in view, distinct tracks so far (>= --min-frames processed frames)

Distinct tracks over-count when one person's track is lost and restarted; the mission's
"people" count also merges tracks by ground position. Watch the video to judge.
No GPS or flight log needed. Processes every --stride-th frame (3 = the pipeline's active
mode); skipped frames reuse the last boxes, so the output keeps the original speed.
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))

GREEN, YELLOW, WHITE, BLACK = (60, 210, 90), (40, 210, 250), (255, 255, 255), (0, 0, 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--weights", default="models/person-visdrone.engine")
    ap.add_argument("--tile", type=int, default=640, help="0 = whole frame only")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--tracker", default="botsort.yaml")
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--min-frames", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=0, help="only the first N seconds (0 = all)")
    ap.add_argument("--device", default="0")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    import cv2

    from rescue_edge.capture import Frame
    from rescue_edge.detector import build_detector

    weights = Path(args.weights)
    if not weights.is_absolute() and not weights.exists() and (ROOT / weights).exists():
        weights = ROOT / weights
    cap = cv2.VideoCapture(str(args.video))
    if not cap.isOpened():
        raise SystemExit(f"cannot open {args.video}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    limit = int(args.seconds * fps) if args.seconds else total
    out = args.out or args.video.with_name(f"{args.video.stem}_annotated.mp4")
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))

    device = int(args.device) if args.device.isdigit() else args.device
    cfg = {"kind": "yolo", "model_path": str(weights), "imgsz": args.imgsz, "conf_threshold": args.conf,
           "device": device, "tracker": args.tracker}
    if args.tile:
        cfg["tile"] = args.tile
    detector = build_detector(cfg)
    print(f"{args.video.name}: {w}x{h}, {fps:.1f} fps, {min(total, limit)} frames | {weights.name}, "
          f"tile {args.tile or 'none'}, {args.tracker}, every {args.stride} frames")

    seen: Counter[int] = Counter()
    dets = []
    scale = max(0.5, w / 1920)
    started = time.perf_counter()
    model_time = 0.0
    i = 0
    while i < limit:
        ok, img = cap.read()
        if not ok:
            break
        if i % args.stride == 0:
            t0 = time.perf_counter()
            dets = detector(Frame(index=i, t=i / fps, image=img, width=w, height=h))
            model_time += time.perf_counter() - t0
            for d in dets:
                if d.track_id is not None:
                    seen[d.track_id] += 1
        for d in dets:
            x1, y1, x2, y2 = (int(v) for v in d.bbox_xyxy)
            color = GREEN if d.confidence >= 0.25 else YELLOW
            cv2.rectangle(img, (x1, y1), (x2, y2), color, max(1, int(2 * scale)))
            if d.track_id is not None:
                cv2.putText(img, str(d.track_id), (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX,
                            0.45 * scale, color, max(1, int(scale)))
        tracks = sum(1 for n in seen.values() if n >= args.min_frames)
        bar = (f"frame {i + 1}/{min(total, limit)}   in view {len(dets)}   "
               f"distinct tracks {tracks}   {weights.name} + {Path(args.tracker).stem}")
        cv2.rectangle(img, (0, 0), (w, int(34 * scale)), BLACK, -1)
        cv2.putText(img, bar, (10, int(23 * scale)), cv2.FONT_HERSHEY_SIMPLEX, 0.6 * scale, WHITE,
                    max(1, int(1.5 * scale)))
        writer.write(img)
        i += 1
        if i % int(fps * 5) == 0:
            print(f"  {i / fps:5.0f} s  in view {len(dets):3d}  distinct tracks {tracks}", flush=True)
    cap.release()
    writer.release()
    processed = (i + args.stride - 1) // args.stride
    tracks = sum(1 for n in seen.values() if n >= args.min_frames)
    print(f"\n{i} frames, {processed} processed, {model_time / max(1, processed) * 1000:.0f} ms per processed "
          f"frame (detector + tracker), {time.perf_counter() - started:.0f} s total")
    print(f"distinct tracks seen in >= {args.min_frames} processed frames: {tracks}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
