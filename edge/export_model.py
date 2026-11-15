#!/usr/bin/env python3
"""Export a small YOLO detection model to a Jetson-local TensorRT FP16 engine."""
from __future__ import annotations

import argparse
import platform
import shutil
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="yolov8n.pt", help="Ultralytics checkpoint or model name")
    parser.add_argument("--output", type=Path, default=Path("models/yolov8n.engine"))
    parser.add_argument("--imgsz", type=int, default=640, help="Fixed square inference size")
    parser.add_argument("--workspace-gb", type=float, default=2.0, help="TensorRT build workspace limit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.imgsz <= 0 or args.workspace_gb <= 0:
        raise SystemExit("--imgsz and --workspace-gb must be positive")
    if args.output.suffix.lower() != ".engine":
        raise SystemExit("--output must end in .engine")
    if platform.machine().lower() not in {"aarch64", "arm64"}:
        raise SystemExit(
            "Build this TensorRT engine on the target Jetson Orin Nano. "
            "TensorRT engines are tied to the device, TensorRT version, and software stack."
        )

    try:
        import tensorrt  # noqa: F401
        import torch
        from ultralytics import YOLO
    except ImportError as exc:
        raise SystemExit(
            "Missing Jetson inference packages. Install the JetPack-compatible PyTorch, "
            "TensorRT, and Ultralytics versions described in docs/hardware.md."
        ) from exc

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is unavailable. Use the JetPack-compatible Jetson PyTorch build.")

    model = YOLO(args.model, task="detect")
    exported = Path(model.export(
        format="engine",
        half=True,
        imgsz=args.imgsz,
        batch=1,
        dynamic=False,
        device=0,
        workspace=args.workspace_gb,
        verbose=True,
    ))
    if not exported.is_file():
        raise SystemExit(f"Ultralytics reported an engine that does not exist: {exported}")

    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if exported.resolve() != output:
        shutil.copy2(exported, output)

    print(f"TensorRT FP16 engine: {output}")
    print(f"Input: fixed 1x3x{args.imgsz}x{args.imgsz}; size: {output.stat().st_size} bytes")
    print("Build and run this engine on the same Jetson / JetPack / TensorRT stack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
