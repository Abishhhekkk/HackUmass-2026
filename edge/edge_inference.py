#!/usr/bin/env python3
"""Run person detection and frame-aligned GPS projection on a Jetson Orin Nano.

Run from the repository root:
    python edge/edge_inference.py --config config/edge.jetson.yaml
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from rescue_edge.capture import CameraSource, Frame, csi_pipeline
from rescue_edge.detector import YoloTracker
from rescue_edge.geolocate import CameraModel, ErrorModel, pixel_to_ground
from rescue_edge.main import load_config
from rescue_edge.telemetry import SerialTelemetryReader, TelemetryBuffer

log = logging.getLogger("rescue_edge.live_inference")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/edge.jetson.yaml"))
    parser.add_argument("--model", help="Override detector.model_path in the config")
    parser.add_argument("--serial-port", help="Override telemetry.port")
    parser.add_argument("--baud", type=int, help="Override telemetry.baud")
    parser.add_argument("--imgsz", type=int, help="Override detector.imgsz")
    parser.add_argument("--confidence", type=float, help="Override detector.conf_threshold")
    parser.add_argument("--output", default="-", help="JSONL output path, or '-' for stdout")
    parser.add_argument(
        "--projection-point",
        choices=("foot", "center"),
        default="foot",
        help="Person ground-contact estimate (recommended), or bounding-box center",
    )
    return parser.parse_args()


def _output_stream(path: str) -> tuple[TextIO, bool]:
    if path == "-":
        return sys.stdout, False
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    return output.open("a", encoding="utf-8", buffering=1), True


def _camera_source(cfg: dict, clock) -> CameraSource:
    source = cfg["source"]
    if source.get("kind") != "camera":
        raise ValueError("This live runner requires source.kind: camera")
    device = source.get("device", 0)
    if device == "csi":
        device = csi_pipeline(
            source.get("width", 1920),
            source.get("height", 1080),
            source.get("fps", 30),
        )
    return CameraSource(device, clock=clock)


def _detection_record(
    frame: Frame,
    detection,
    telemetry: TelemetryBuffer,
    camera: CameraModel,
    error_model: ErrorModel,
    projection_point: str,
) -> dict:
    timestamp = datetime.fromtimestamp(frame.t, tz=timezone.utc).isoformat(timespec="milliseconds")
    pixel = detection.foot_pixel if projection_point == "foot" else detection.centre_pixel
    lookup = telemetry.at(frame.t)
    location = None
    reason = lookup.reason
    horizontal_error_m = None

    if frame.width != camera.width or frame.height != camera.height:
        reason = "camera_resolution_mismatch"
    elif lookup.pose is not None:
        result = pixel_to_ground(*pixel, lookup.pose, camera, error_model)
        location = {"lat": result.lat, "lon": result.lon} if result.ok else None
        horizontal_error_m = result.horizontal_error_m
        reason = result.reason

    return {
        "object_class": "person",
        "confidence": round(float(detection.confidence), 6),
        "target_lat": location["lat"] if location else None,
        "target_lon": location["lon"] if location else None,
        "timestamp": timestamp,
        "horizontal_error_m": horizontal_error_m,
        "position_method": "nadir_flat_ground_v1",
        "unlocated_reason": reason,
    }


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if args.imgsz is not None and args.imgsz <= 0:
        raise SystemExit("--imgsz must be positive")
    if args.confidence is not None and not 0 < args.confidence < 1:
        raise SystemExit("--confidence must be between 0 and 1")
    if args.baud is not None and args.baud <= 0:
        raise SystemExit("--baud must be positive")

    cfg, base_dir = load_config(str(args.config))
    telemetry_cfg = cfg["telemetry"]
    if telemetry_cfg.get("kind") != "serial":
        raise SystemExit("Set telemetry.kind to 'serial' in the selected config.")

    def resolve(path: str) -> str:
        candidate = Path(path).expanduser()
        return str(candidate if candidate.is_absolute() else base_dir / candidate)

    model_path = resolve(args.model or cfg["detector"]["model_path"])
    if Path(model_path).suffix.lower() != ".engine":
        raise SystemExit(f"Expected a TensorRT .engine model, got: {model_path}")
    model_file = Path(model_path)
    if not model_file.is_file():
        raise SystemExit(
            f"TensorRT engine not found: {model_file}. Export it on this Jetson with "
            "`python edge/export_model.py` first."
        )

    camera = CameraModel.from_config(cfg["camera"])
    error_model = ErrorModel(**cfg.get("error_model", {}))
    detector = YoloTracker(
        model_path=model_path,
        imgsz=args.imgsz or cfg["detector"].get("imgsz", 640),
        conf_threshold=args.confidence or cfg["detector"].get("conf_threshold", 0.35),
        device=cfg["detector"].get("device", 0),
        tracker=cfg["detector"].get("tracker", "bytetrack.yaml"),
        half=False,
    )
    telemetry = TelemetryBuffer(
        telemetry_cfg.get("max_gap_s", 1.0),
        telemetry_cfg.get("max_age_s", 2.0),
    )
    reader = SerialTelemetryReader(
        args.serial_port or telemetry_cfg["port"],
        args.baud or telemetry_cfg.get("baud", 115200),
        telemetry,
        resolve(telemetry_cfg["log_path"]) if telemetry_cfg.get("log_path") else None,
    )
    output, close_output = _output_stream(args.output)
    frame_count = 0
    detection_count = 0
    last_stats = time.monotonic()

    try:
        reader.start()
        source = _camera_source(cfg, reader.now)
        log.info(
            "starting live inference: model=%s imgsz=%d camera=%s serial=%s",
            model_path,
            args.imgsz or cfg["detector"].get("imgsz", 640),
            cfg["source"].get("device", 0),
            reader.port,
        )
        for frame in source:
            frame_count += 1
            for detection in detector(frame):
                record = _detection_record(
                    frame, detection, telemetry, camera, error_model, args.projection_point
                )
                output.write(json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n")
                output.flush()
                detection_count += 1
            now = time.monotonic()
            if now - last_stats >= 10:
                log.info(
                    "processed %d frames, emitted %d detections, buffered %d telemetry samples",
                    frame_count,
                    detection_count,
                    len(telemetry),
                )
                if reader.last_error:
                    log.warning("telemetry reader: %s", reader.last_error)
                last_stats = now
    except KeyboardInterrupt:
        log.info("stopping inference on keyboard interrupt")
    finally:
        if reader.is_alive():
            reader.stop()
            reader.join(timeout=2.0)
            if reader.is_alive():
                log.error("telemetry reader did not stop within 2 seconds")
        if close_output:
            output.close()

    log.info("finished: %d frames, %d detections", frame_count, detection_count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
