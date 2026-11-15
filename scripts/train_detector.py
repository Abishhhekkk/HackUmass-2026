#!/usr/bin/env python3
"""Fine-tune YOLOv8n on the tiled single-class person dataset (run on a machine with a GPU).

    python scripts/train_detector.py --data datasets/visdrone-person/data.yaml --epochs 50

Run it on a laptop with an NVIDIA GPU or a Vultr cloud GPU, NOT on the Jetson during the
demo. The result is runs/rescue/<name>/weights/best.pt (~6 MB): copy it to the Jetson and
build the TensorRT engine there (docs/training.md). The model has one class, person = 0, so
the edge config needs no class changes.

Defaults: start from COCO-pretrained yolov8n.pt (transfer learning), imgsz 640 = the Jetson
engine size, early stopping after 15 epochs without improvement.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", type=Path, required=True, help="data.yaml from prepare_visdrone.py")
    ap.add_argument("--model", default="yolov8n.pt", help="starting weights (COCO-pretrained)")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--imgsz", type=int, default=640, help="must equal the Jetson engine size")
    ap.add_argument("--batch", type=int, default=-1, help="-1 = pick the largest that fits the GPU")
    ap.add_argument("--device", default="0", help="CUDA index, or 'cpu' (only for a smoke test)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--patience", type=int, default=15)
    ap.add_argument("--name", default="person-yolov8n")
    ap.add_argument("--project", type=Path, default=Path("runs/rescue"))
    ap.add_argument("--fraction", type=float, default=1.0, help="use part of the train set (quick test)")
    args = ap.parse_args()

    if not args.data.is_file():
        raise SystemExit(f"{args.data} not found; run scripts/prepare_visdrone.py first")
    try:
        import torch
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit("pip install ultralytics (with a CUDA build of PyTorch on the GPU machine)") from exc
    if args.device != "cpu" and not torch.cuda.is_available():
        raise SystemExit("No CUDA GPU visible to PyTorch. Install the CUDA build of PyTorch, or use "
                         "--device cpu for a smoke test only (far too slow for real training).")
    if args.device != "cpu":
        print(f"GPU: {torch.cuda.get_device_name(int(args.device.split(',')[0]))}")

    model = YOLO(args.model, task="detect")
    model.train(
        data=str(args.data.resolve()), epochs=args.epochs, imgsz=args.imgsz,
        batch=args.batch if args.device != "cpu" else max(args.batch, 4),
        device=args.device, workers=args.workers, patience=args.patience,
        project=str(args.project.resolve()), name=args.name, exist_ok=False,
        fraction=args.fraction, plots=True,  # data.yaml already has one class, 'person'
    )
    best = Path(model.trainer.best)
    print(f"\nbest weights: {best}  ({best.stat().st_size / 1e6:.1f} MB)")
    print("Next: evaluate it like the stock model (docs/detector-eval.md), then copy best.pt to\n"
          "the Jetson and build the engine there (docs/training.md, 'On the Jetson').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
