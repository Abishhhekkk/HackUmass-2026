"""Frame sources. All yield Frame objects with a GPS-clock timestamp.

* VideoFileSource  - prerecorded clip; t = video_start_time + position in clip
* CameraSource     - live camera (USB index, or a GStreamer pipeline for CSI cameras)
* ReplaySource     - no pixels at all; replays a detections .jsonl (mock mode / CI)
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np


@dataclass
class Frame:
    index: int
    t: float                       # UTC epoch seconds on the GPS clock
    image: np.ndarray | None = None
    width: int = 0
    height: int = 0
    meta: dict = field(default_factory=dict)


class VideoFileSource:
    def __init__(self, path: str, video_start_time: float, realtime: bool = False):
        self.path, self.start, self.realtime = path, video_start_time, realtime

    def __iter__(self) -> Iterator[Frame]:
        import cv2

        cap = cv2.VideoCapture(self.path)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open video {self.path}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        i, wall0 = 0, time.monotonic()
        try:
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 or i / fps
                if self.realtime:
                    delay = pos - (time.monotonic() - wall0)
                    if delay > 0:
                        time.sleep(delay)
                yield Frame(i, self.start + pos, img, img.shape[1], img.shape[0])
                i += 1
        finally:
            cap.release()


def csi_pipeline(width: int = 1920, height: int = 1080, fps: int = 30, sensor_id: int = 0) -> str:
    """GStreamer pipeline for a CSI camera (e.g. IMX219) on Jetson."""
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} ! video/x-raw(memory:NVMM),width={width},"
        f"height={height},framerate={fps}/1 ! nvvidconv ! video/x-raw,format=BGRx ! "
        "videoconvert ! video/x-raw,format=BGR ! appsink drop=1 max-buffers=1"
    )


class CameraSource:
    def __init__(self, device: int | str, clock: Callable[[], float] = time.time):
        self.device, self.clock = device, clock

    def __iter__(self) -> Iterator[Frame]:
        import cv2

        api = cv2.CAP_GSTREAMER if isinstance(self.device, str) and "!" in self.device else cv2.CAP_ANY
        cap = cv2.VideoCapture(self.device, api)
        if not cap.isOpened():
            raise RuntimeError(f"cannot open camera {self.device}")
        i = 0
        try:
            while True:
                ok, img = cap.read()
                t = self.clock()
                if not ok:
                    time.sleep(0.01)
                    continue
                yield Frame(i, t, img, img.shape[1], img.shape[0])
                i += 1
        finally:
            cap.release()


class ReplaySource:
    """Mock source: each .jsonl line is {"frame": i, "t": ..., "width": W, "height": H,
    "detections": [{"bbox": [x1,y1,x2,y2], "conf": 0.8, "track_id": 3}, ...]}"""

    def __init__(self, path: str | Path, realtime: bool = False, speed: float = 1.0):
        self.path, self.realtime, self.speed = Path(path), realtime, speed

    def __iter__(self) -> Iterator[Frame]:
        t0 = wall0 = None
        with open(self.path) as f:
            for line in f:
                d = json.loads(line)
                if self.realtime:
                    if t0 is None:
                        t0, wall0 = d["t"], time.monotonic()
                    delay = (d["t"] - t0) / self.speed - (time.monotonic() - wall0)
                    if delay > 0:
                        time.sleep(delay)
                yield Frame(d["frame"], d["t"], None, d["width"], d["height"],
                            meta={"detections": d["detections"]})
