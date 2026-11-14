#!/usr/bin/env python3
"""Evaluate the person detector on a labelled image set across image sizes and thresholds.

    python scripts/eval_detector.py --images VisDrone2019-DET-val/images \\
        --labels VisDrone2019-DET-val/annotations --format visdrone \\
        --weights yolov8n.pt --imgsz 640 960 1280 --conf 0.1 0.25 0.35

The model runs once per image size at the lowest threshold; every threshold is then scored
from those predictions. Person-only, IoU 0.5, recall first. ``--model-classes`` are the
detector's person class IDs: 0 for stock COCO YOLO (default), e.g. ``0 1`` for weights
trained on VisDrone (pedestrian + people). Speeds are Ultralytics' per-image inference
times on THIS machine; measure on the Jetson for demo numbers.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
from rescue_edge.detector_eval import Counts, add, load_truth, match_image  # noqa: E402

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


def fmt(v: float | None, pct: bool = True) -> str:
    return "-" if v is None else (f"{100 * v:.1f}%" if pct else f"{v:.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", type=Path, required=True)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--format", choices=("visdrone", "yolo", "voc"), required=True)
    ap.add_argument("--weights", default="yolov8n.pt")
    ap.add_argument("--imgsz", type=int, nargs="+", default=[640, 960, 1280])
    ap.add_argument("--conf", type=float, nargs="+", default=[0.1, 0.25, 0.35])
    ap.add_argument("--iou", type=float, default=0.5)
    ap.add_argument("--model-classes", type=int, nargs="+", default=[0],
                    help="detector class IDs counted as person (COCO: 0)")
    ap.add_argument("--label-classes", nargs="+",
                    help="ground-truth person classes (default per format: visdrone 1 2, yolo 0, "
                         "voc human/person/pedestrian/people)")
    ap.add_argument("--device", default="cpu", help="'cpu' or a CUDA index such as 0")
    ap.add_argument("--limit", type=int, help="evaluate only the first N images (quick check)")
    ap.add_argument("--csv", type=Path, help="also write the results table to this CSV")
    args = ap.parse_args()

    if not 0 < args.iou <= 1 or any(not 0 <= c < 1 for c in args.conf):
        raise SystemExit("--iou must be in (0, 1] and --conf values in [0, 1)")
    images = sorted(p for p in args.images.iterdir() if p.suffix.lower() in IMAGE_EXTS)[: args.limit]
    if not images:
        raise SystemExit(f"no images in {args.images}")
    label_classes = None
    if args.label_classes:
        label_classes = (tuple(args.label_classes) if args.format == "voc"
                         else tuple(int(c) for c in args.label_classes))
    missing = sum(not (args.labels / (p.stem + (".xml" if args.format == "voc" else ".txt"))).is_file()
                  for p in images)
    if missing == len(images):
        raise SystemExit(f"no label files in {args.labels} match the images; check --labels/--format")

    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("pip install ultralytics (see docs/hardware.md)") from exc
    model = YOLO(args.weights, task="detect")
    device = int(args.device) if args.device.isdigit() else args.device
    floor = min(args.conf)

    rows = []
    for size in args.imgsz:
        totals = {c: Counts() for c in args.conf}
        ms = []
        for n, path in enumerate(images, 1):
            res = model.predict(str(path), imgsz=size, conf=floor, classes=args.model_classes,
                                device=device, verbose=False)[0]
            h, w = res.orig_shape
            ms.append(res.speed.get("inference", 0.0))
            preds = [(tuple(b), float(s)) for b, s in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist())]
            truth = load_truth(args.labels, path, args.format, w, h, label_classes)
            for c in args.conf:
                add(totals[c], match_image(preds, truth, c, args.iou))
            if n % 50 == 0:
                print(f"  imgsz {size}: {n}/{len(images)} images", file=sys.stderr)
        for c in args.conf:
            t = totals[c]
            rows.append({"imgsz": size, "conf": c, "images": t.images, "gt_people": t.tp + t.fn,
                         "tp": t.tp, "fp": t.fp, "fn": t.fn,
                         "recall": t.recall, "precision": t.precision, "f1": t.f1,
                         "small_gt": t.gt_small, "recall_small": t.recall_small,
                         "inference_ms_median": statistics.median(ms)})

    print(f"\n{len(images)} images, {args.format} labels, IoU {args.iou}, weights {args.weights}, "
          f"device {args.device}" + (f", {missing} images without a label file" if missing else ""))
    print("| imgsz | conf | recall | precision | F1 | recall (small <32px) | TP | FP | FN | ms/img |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda r: (-(r["recall"] or 0), r["imgsz"])):
        print(f"| {r['imgsz']} | {r['conf']:g} | {fmt(r['recall'])} | {fmt(r['precision'])} | "
              f"{fmt(r['f1'])} | {fmt(r['recall_small'])} | {r['tp']} | {r['fp']} | {r['fn']} | "
              f"{fmt(r['inference_ms_median'], pct=False)} |")
    print("Sorted by recall (the priority metric). ms/img = median inference time on this machine.")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
