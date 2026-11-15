#!/usr/bin/env python3
"""Is the people COUNT right? Run the edge detector+tracker on aerial video with person IDs.

    python scripts/eval_counting.py --root datasets/VisDrone2019-MOT-val \\
        --weights models/person-visdrone.engine --tile 640 --conf 0.1 --stride 3

``--root`` holds ``sequences/<name>/*.jpg`` and ``annotations/<name>.txt`` (VisDrone-MOT).
Same detector, tiling and ByteTrack as the pipeline (rescue_edge.detector.build_detector);
``--stride`` = run on every Nth frame like the scheduler (3 = active, 4 = search).
A person counts once tracked in ``--min-frames`` evaluated frames (the aggregator's default).
Tracks are scored in image space: the pipeline additionally merges tracks whose ground
positions are within ``merge_radius_m``, which can only lower its count. See count_eval.py.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "edge"))
from rescue_edge.count_eval import CountResult, parse_mot, score_sequence  # noqa: E402


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--weights", required=True)
    ap.add_argument("--tile", type=int, default=640, help="0 = whole frame (no tiling)")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--conf", type=float, default=0.10)
    ap.add_argument("--stride", type=int, default=3)
    ap.add_argument("--min-frames", type=int, default=3)
    ap.add_argument("--max-frames", type=int, default=0, help="per sequence, before stride; 0 = all")
    ap.add_argument("--sequences", nargs="*", help="only these sequence names")
    ap.add_argument("--tracker", default="bytetrack.yaml",
                    help="bytetrack.yaml, botsort.yaml (camera-motion compensation) or a path")
    ap.add_argument("--device", default="0")
    args = ap.parse_args()

    import cv2

    from rescue_edge.capture import Frame
    from rescue_edge.detector import build_detector

    seq_dir, ann_dir = args.root / "sequences", args.root / "annotations"
    names = sorted(p.name for p in seq_dir.iterdir() if p.is_dir())
    if args.sequences:
        names = [n for n in names if n in set(args.sequences)]
    if not names:
        raise SystemExit(f"no sequences under {seq_dir}")
    device = int(args.device) if args.device.isdigit() else args.device
    cfg = {"kind": "yolo", "model_path": args.weights, "imgsz": args.imgsz,
           "conf_threshold": args.conf, "device": device, "tracker": args.tracker}
    if args.tile:
        cfg["tile"] = args.tile
    detector = None

    print(f"{args.weights} + {args.tracker}: tile {args.tile or 'none'}, conf {args.conf}, every {args.stride} frames, "
          f"person = tracked in >= {args.min_frames} evaluated frames\n")
    print("| sequence | frames | true people | counted | found | false | duplicate | count error |")
    print("|---|---|---|---|---|---|---|---|")
    total = CountResult(sequence="ALL")
    start = time.perf_counter()
    for name in names:
        truth = parse_mot((ann_dir / f"{name}.txt").read_text())
        images = sorted((seq_dir / name).glob("*.jpg"))
        if args.max_frames:
            images = images[:args.max_frames]
        if detector is None:
            detector = build_detector(cfg)          # load the model once
        else:
            detector.reset_tracks()                 # fresh track IDs per sequence
        tracks: dict[int, list] = {}
        for i, path in enumerate(images):
            if i % args.stride:
                continue
            img = cv2.imread(str(path))
            fnum = int(path.stem)                   # VisDrone frame files: 0000001.jpg
            dets = detector(Frame(index=i, t=0.0, image=img, width=img.shape[1], height=img.shape[0]))
            tracks[fnum] = [(d.track_id, d.bbox_xyxy) for d in dets if d.track_id is not None]
        r = score_sequence(tracks, truth, args.min_frames, name)
        total.add(r)
        print(f"| {name} | {r.frames} | {r.gt_people} | {r.counted} | {r.found} ({pct(r.found, r.gt_people)}) "
              f"| {r.false_tracks} | {r.duplicates} | {r.count_error:+d} |", flush=True)
    t = total
    print(f"| **all** | {t.frames} | {t.gt_people} | {t.counted} | {t.found} ({pct(t.found, t.gt_people)}) "
          f"| {t.false_tracks} | {t.duplicates} | {t.count_error:+d} |")
    print(f"\nfound = true people with at least one correct track; false = tracks on no person; "
          f"duplicate = extra tracks on an already-counted person. {time.perf_counter() - start:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
