#!/usr/bin/env python3
"""Convert VisDrone (DET) to a tiled, single-class 'person' YOLO dataset for fine-tuning.

    python scripts/prepare_visdrone.py \\
        --train VisDrone2019-DET-train --val VisDrone2019-DET-val --out datasets/visdrone-person

Each split folder holds images/ and annotations/ (the official layout). Output:

    <out>/images/{train,val}/*.jpg   640 px tiles at native resolution (20% overlap)
    <out>/labels/{train,val}/*.txt   class 0 = person (VisDrone pedestrian + people)
    <out>/data.yaml                  for Ultralytics: one class, 'person'

VisDrone "ignored regions" and people cut to under half their size by a tile edge are
painted grey, so the model is neither rewarded nor punished for them. Most tiles without
people are dropped (``--empty-ratio`` keeps some, so the model still sees empty ground).
VisDrone is for non-commercial research use; check its licence before any other use.
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
from rescue_edge.dataset import boxes_in_tile, regions_in_tile, yolo_label_lines  # noqa: E402
from rescue_edge.detector_eval import parse_visdrone  # noqa: E402
from rescue_edge.tiling import tile_grid  # noqa: E402

GREY = (114, 114, 114)  # Ultralytics' letterbox fill


def _overlaps(box, others) -> bool:
    return any(min(box[2], o[2]) > max(box[0], o[0]) and min(box[3], o[3]) > max(box[1], o[1])
               for o in others)


def convert_split(src: Path, out: Path, split: str, tile: int, overlap: float,
                  empty_ratio: float, limit: int | None, rng: random.Random) -> tuple[int, int, int]:
    import cv2

    img_dir, ann_dir = src / "images", src / "annotations"
    if not img_dir.is_dir() or not ann_dir.is_dir():
        raise SystemExit(f"{src} must contain images/ and annotations/")
    (out / "images" / split).mkdir(parents=True, exist_ok=True)
    (out / "labels" / split).mkdir(parents=True, exist_ok=True)
    images = sorted(img_dir.glob("*.jpg"))[:limit]
    n_tiles = n_people = n_empty = 0
    for i, path in enumerate(images, 1):
        ann = ann_dir / (path.stem + ".txt")
        truth = parse_visdrone(ann.read_text()) if ann.is_file() else None
        if truth is None:
            continue
        image = cv2.imread(str(path))
        if image is None:
            print(f"skipping unreadable {path.name}", file=sys.stderr)
            continue
        h, w = image.shape[:2]
        for wi, window in enumerate(tile_grid(w, h, tile, overlap)):
            kept, partial = boxes_in_tile(truth.people, window)
            if not kept and rng.random() >= empty_ratio:
                continue
            x0, y0, x1, y1 = window
            crop = image[y0:y1, x0:x1].copy()
            # never paint over a labelled person (crowds): a cut-off neighbour that overlaps
            # one is left as is instead
            blank = [b for b in regions_in_tile(truth.ignore, window) + partial if not _overlaps(b, kept)]
            for rx1, ry1, rx2, ry2 in blank:
                cv2.rectangle(crop, (int(rx1), int(ry1)), (int(rx2), int(ry2)), GREY, thickness=-1)
            name = f"{path.stem}_t{wi:02d}"
            cv2.imwrite(str(out / "images" / split / f"{name}.jpg"), crop,
                        [cv2.IMWRITE_JPEG_QUALITY, 95])
            th, tw = crop.shape[:2]
            (out / "labels" / split / f"{name}.txt").write_text(
                "\n".join(yolo_label_lines(kept, tw, th)) + ("\n" if kept else ""))
            n_tiles += 1
            n_people += len(kept)
            n_empty += not kept
        if i % 500 == 0:
            print(f"  {split}: {i}/{len(images)} images", file=sys.stderr)
    return n_tiles, n_people, n_empty


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", type=Path, required=True, help="VisDrone2019-DET-train folder")
    ap.add_argument("--val", type=Path, required=True, help="VisDrone2019-DET-val folder")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tile", type=int, default=640, help="tile size in native pixels (= engine size)")
    ap.add_argument("--overlap", type=float, default=0.2)
    ap.add_argument("--empty-ratio", type=float, default=0.1, help="share of empty tiles to keep")
    ap.add_argument("--limit", type=int, help="first N images per split (quick test)")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        raise SystemExit(f"{args.out} is not empty; choose a new --out")
    rng = random.Random(args.seed)
    for split, src in (("train", args.train), ("val", args.val)):
        t, p, e = convert_split(src, args.out, split, args.tile, args.overlap,
                                args.empty_ratio, args.limit, rng)
        print(f"{split}: {t} tiles, {p} labelled people, {e} empty tiles")
    (args.out / "data.yaml").write_text(
        f"path: {args.out.resolve()}\ntrain: images/train\nval: images/val\n"
        "nc: 1\nnames: [person]\n")
    print(f"wrote {args.out / 'data.yaml'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
