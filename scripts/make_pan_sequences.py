#!/usr/bin/env python3
"""Build counting-test sequences from VisDrone-DET images (no Google Drive needed).

    python scripts/make_pan_sequences.py --det datasets/VisDrone2019-DET-val \\
        --out datasets/visdrone-pan --images 20 --frames 60

Each DET image becomes a short "flight": the camera pans slowly across it, and every labelled
person keeps one ID for the whole sequence. Output is VisDrone-MOT format, so
scripts/eval_counting.py runs on it unchanged.

What it tests: whether one person turns into several counted people (split boxes, tracks
lost and restarted) and how many real people are counted, on real aerial imagery.
What it does NOT test: people moving, occlusion over time, motion blur, changing altitude.
Use VisDrone-MOT for that when you can download it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--det", type=Path, required=True, help="VisDrone2019-DET-val folder (images/, annotations/)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--images", type=int, default=20, help="how many images (most people first)")
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--pan", type=float, default=0.12, help="total pan as a share of the image width")
    args = ap.parse_args()

    import cv2

    ann_dir, img_dir = args.det / "annotations", args.det / "images"
    picked = []
    for ann in sorted(ann_dir.glob("*.txt")):
        rows = [r.split(",") for r in ann.read_text().split()]
        people = sum(1 for r in rows if len(r) >= 6 and r[5] in ("1", "2") and r[4] == "1")
        if people >= 5:
            picked.append((people, ann.stem, rows))
    picked.sort(key=lambda t: -t[0])
    picked = picked[: args.images]
    if not picked:
        raise SystemExit(f"no annotated images with people under {ann_dir}")

    (args.out / "annotations").mkdir(parents=True, exist_ok=True)
    total = 0
    for people, stem, rows in picked:
        img = cv2.imread(str(img_dir / f"{stem}.jpg"))
        if img is None:
            continue
        h, w = img.shape[:2]
        pan = int(w * args.pan)
        crop_w = w - pan
        seq = args.out / "sequences" / stem
        seq.mkdir(parents=True, exist_ok=True)
        lines = []
        for f in range(1, args.frames + 1):
            dx = round(pan * (f - 1) / max(1, args.frames - 1))
            cv2.imwrite(str(seq / f"{f:07d}.jpg"), img[:, dx:dx + crop_w], [cv2.IMWRITE_JPEG_QUALITY, 92])
            for pid, r in enumerate(rows):
                if len(r) < 6:
                    continue
                x, y, bw, bh, score, cat = (int(v) for v in r[:6])
                if cat not in (0, 1, 2):
                    continue
                x0, x1 = max(x - dx, 0), min(x + bw - dx, crop_w)
                if x1 - x0 < max(2, bw // 2):      # mostly outside this frame
                    continue
                lines.append(f"{f},{pid},{x0},{y},{x1 - x0},{bh},{score if cat else 0},{cat},0,0")
        (args.out / "annotations" / f"{stem}.txt").write_text("\n".join(lines) + "\n")
        total += people
        print(f"{stem}: {people} people, {args.frames} frames")
    print(f"\n{len(picked)} sequences, {total} labelled people -> {args.out}")
    print(f"next: python scripts/eval_counting.py --root {args.out} --weights models/person-visdrone.engine --device 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
