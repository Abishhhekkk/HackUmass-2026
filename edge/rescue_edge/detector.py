"""Person detection + per-pass tracking.

YoloTracker wraps Ultralytics (YOLOv8n / YOLO11n, .pt or TensorRT .engine) with ByteTrack.
MockDetector returns the detections embedded by ReplaySource, so everything downstream
can be developed and tested without a GPU.

track_id is a temporary ID inside one continuous pass. It is NOT a person identity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .capture import Frame

PERSON_CLASS = 0  # COCO


@dataclass(frozen=True)
class Detection:
    bbox_xyxy: tuple[float, float, float, float]
    confidence: float
    track_id: int | None

    @property
    def foot_pixel(self) -> tuple[float, float]:
        """Bottom-centre of the box: tentative ground contact point."""
        x1, _, x2, y2 = self.bbox_xyxy
        return (x1 + x2) / 2, y2

    @property
    def centre_pixel(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox_xyxy
        return (x1 + x2) / 2, (y1 + y2) / 2


class Detector(Protocol):
    def __call__(self, frame: Frame) -> list[Detection]: ...


class MockDetector:
    def __init__(self, conf_threshold: float = 0.35):
        self.conf = conf_threshold

    def __call__(self, frame: Frame) -> list[Detection]:
        return [
            Detection(tuple(d["bbox"]), float(d["conf"]), d.get("track_id"))
            for d in frame.meta.get("detections", []) if d["conf"] >= self.conf
        ]


class YoloTracker:
    def __init__(self, model_path: str = "yolov8n.pt", imgsz: int = 960,
                 conf_threshold: float = 0.35, device: str | int = 0,
                 tracker: str = "bytetrack.yaml", half: bool = True):
        try:
            from ultralytics import YOLO
        except ImportError as e:
            raise RuntimeError("ultralytics not installed; use detector.kind=mock or "
                               "pip install ultralytics (see docs/hardware.md)") from e
        self.model = YOLO(model_path, task="detect")
        self.imgsz, self.conf, self.device = imgsz, conf_threshold, device
        self.tracker, self.half = tracker, half and not str(model_path).endswith(".engine")

    def __call__(self, frame: Frame) -> list[Detection]:
        res = self.model.track(
            frame.image, persist=True, classes=[PERSON_CLASS], conf=self.conf,
            imgsz=self.imgsz, device=self.device, tracker=self.tracker,
            half=self.half, verbose=False,
        )[0]
        boxes = res.boxes
        if boxes is None or len(boxes) == 0:
            return []
        ids = boxes.id.int().tolist() if boxes.id is not None else [None] * len(boxes)
        return [
            Detection(tuple(float(v) for v in xyxy), float(c), tid)
            for xyxy, c, tid in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), ids)
        ]

    def reset_tracks(self) -> None:
        """Call between passes/videos so IDs don't leak across them."""
        for p in getattr(self.model.predictor, "trackers", []) or []:
            p.reset()


def build_detector(cfg: dict) -> Detector:
    kind = cfg.get("kind", "mock")
    if kind == "mock":
        return MockDetector(cfg.get("conf_threshold", 0.35))
    if kind == "yolo":
        return YoloTracker(
            model_path=cfg.get("model_path", "yolov8n.pt"),
            imgsz=cfg.get("imgsz", 960),
            conf_threshold=cfg.get("conf_threshold", 0.35),
            device=cfg.get("device", 0),
            tracker=cfg.get("tracker", "bytetrack.yaml"),
        )
    raise ValueError(f"unknown detector kind {kind!r}")
