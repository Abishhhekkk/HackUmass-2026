"""Person detection + per-pass tracking.

YoloTracker wraps Ultralytics (YOLOv8n / YOLO11n, .pt or TensorRT .engine) with ByteTrack.
TiledYoloTracker runs the same model on overlapping tiles (small, distant people keep their
pixels; see rescue_edge/tiling.py and docs/detector-eval.md) and ByteTrack on the merged boxes.
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


class TiledYoloTracker:
    """YOLO on overlapping tiles + one full-frame pass, merged, then ByteTrack.

    Ultralytics' ``model.track()`` only sees whole frames, so tracking is done here with the
    same ByteTrack implementation and config (``bytetrack.yaml``) on the merged detections.
    The model input size stays fixed (``imgsz``, e.g. the 640 TensorRT engine); ``tile`` is
    the crop size in frame pixels, so tile < imgsz enlarges people (320 -> 640 = 2x).
    Cost: one model run per tile (+1 full frame) per processed frame.
    """

    def __init__(self, model_path: str = "yolov8n.pt", imgsz: int = 640,
                 conf_threshold: float = 0.1, device: str | int = 0,
                 tracker: str = "bytetrack.yaml", tile: int = 320, tile_overlap: float = 0.2,
                 full_frame: bool = True, half: bool = True, frame_rate: int = 30,
                 model=None, tracker_impl=None):
        from .tiling import tile_grid  # validates tile/overlap early

        tile_grid(tile, tile, tile, tile_overlap)
        self.imgsz, self.conf, self.device = imgsz, conf_threshold, device
        self.tile, self.tile_overlap, self.full_frame = tile, tile_overlap, full_frame
        self.half = half and not str(model_path).endswith(".engine")
        if model is None or tracker_impl is None:
            try:
                from ultralytics import YOLO
                from ultralytics.trackers.bot_sort import BOTSORT
                from ultralytics.trackers.byte_tracker import BYTETracker
                from ultralytics.utils import YAML, IterableSimpleNamespace
                from ultralytics.utils.checks import check_yaml
            except ImportError as e:
                raise RuntimeError("ultralytics not installed; use detector.kind=mock or "
                                   "pip install ultralytics (see docs/hardware.md)") from e
            if model is None:
                model = YOLO(model_path, task="detect")
            if tracker_impl is None:
                cfg = IterableSimpleNamespace(**YAML.load(check_yaml(tracker)))
                if cfg.tracker_type == "bytetrack":
                    tracker_impl = BYTETracker(args=cfg, frame_rate=frame_rate)
                elif cfg.tracker_type == "botsort":
                    if getattr(cfg, "with_reid", False):
                        raise ValueError("tiled detection: set with_reid: False in the BoT-SORT config")
                    # BoT-SORT = ByteTrack + camera-motion compensation (gmc_method), which keeps
                    # small people tracked while the drone moves.
                    tracker_impl = BOTSORT(args=cfg, frame_rate=frame_rate)
                else:
                    raise ValueError(f"unsupported tracker_type {cfg.tracker_type!r}")
        self.model, self._tracker = model, tracker_impl

    def _predict(self, crop):
        r = self.model.predict(crop, imgsz=self.imgsz, conf=self.conf, classes=[PERSON_CLASS],
                               device=self.device, half=self.half, verbose=False)[0]
        return r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()

    def detect(self, image):
        """Merged tile detections in frame pixels: (boxes Nx4, scores N)."""
        from .tiling import predict_tiled

        return predict_tiled(self._predict, image, self.tile, self.tile_overlap, self.full_frame)

    def __call__(self, frame: Frame) -> list[Detection]:
        import numpy as np

        boxes, scores = self.detect(frame.image)
        data = (np.column_stack([boxes, scores, np.zeros(len(scores))]) if len(scores)
                else np.zeros((0, 6)))
        tracks = self._tracker.update(_boxes(data, frame.image.shape[:2]), frame.image)
        # rows: x1, y1, x2, y2, track_id, score, cls, det_index (same as model.track)
        return [Detection(tuple(float(v) for v in row[:4]), float(row[5]), int(row[4]))
                for row in np.asarray(tracks).reshape(-1, 8)]

    def reset_tracks(self) -> None:
        self._tracker.reset()


def _boxes(data, orig_shape):
    """Ultralytics Boxes (what ByteTrack consumes); falls back to the raw array in tests."""
    try:
        from ultralytics.engine.results import Boxes
    except ImportError:
        return data
    return Boxes(data, orig_shape)


def build_detector(cfg: dict) -> Detector:
    kind = cfg.get("kind", "mock")
    if kind == "mock":
        return MockDetector(cfg.get("conf_threshold", 0.35))
    if kind == "yolo" and cfg.get("tile"):
        return TiledYoloTracker(
            model_path=cfg.get("model_path", "yolov8n.pt"),
            imgsz=cfg.get("imgsz", 640),
            conf_threshold=cfg.get("conf_threshold", 0.1),
            device=cfg.get("device", 0),
            tracker=cfg.get("tracker", "bytetrack.yaml"),
            tile=int(cfg["tile"]),
            tile_overlap=cfg.get("tile_overlap", 0.2),
            full_frame=cfg.get("tile_full_frame", True),
        )
    if kind == "yolo":
        return YoloTracker(
            model_path=cfg.get("model_path", "yolov8n.pt"),
            imgsz=cfg.get("imgsz", 960),
            conf_threshold=cfg.get("conf_threshold", 0.35),
            device=cfg.get("device", 0),
            tracker=cfg.get("tracker", "bytetrack.yaml"),
        )
    raise ValueError(f"unknown detector kind {kind!r}")
