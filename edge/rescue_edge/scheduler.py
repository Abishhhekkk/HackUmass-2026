"""Adaptive frame scheduler: the concrete form of "dynamic GPU allocation".

The GPU budget is spent where it matters:
* SEARCH mode (nobody in view): run the detector on every Nth frame.
* ACTIVE mode (people in view): run on every frame so tracks stay continuous.
* THERMAL mode (Jetson too hot): back off regardless, to avoid throttling.

We vary frame stride rather than input size because a TensorRT engine is built for a
fixed input size; resizing would force a rebuild or a second engine.
"""
from __future__ import annotations

import glob
import time
from dataclasses import dataclass


def read_max_temp_c() -> float | None:
    temps = []
    for p in glob.glob("/sys/devices/virtual/thermal/thermal_zone*/temp"):
        try:
            temps.append(int(open(p).read().strip()) / 1000.0)
        except (OSError, ValueError):
            pass
    return max(temps) if temps else None


@dataclass
class SchedulerConfig:
    search_stride: int = 4
    active_stride: int = 1
    thermal_stride: int = 6
    hold_active_s: float = 3.0     # stay ACTIVE this long after the last detection
    thermal_limit_c: float = 80.0
    thermal_check_s: float = 5.0


class AdaptiveScheduler:
    def __init__(self, cfg: SchedulerConfig = SchedulerConfig(), temp_reader=read_max_temp_c):
        self.cfg, self.read_temp = cfg, temp_reader
        self.mode = "search"
        self._last_hit_t: float | None = None
        self._next_frame = 0
        self._temp: float | None = None
        self._temp_checked = 0.0
        self.mode_frames = {"search": 0, "active": 0, "thermal": 0}

    @property
    def stride(self) -> int:
        return {"search": self.cfg.search_stride, "active": self.cfg.active_stride,
                "thermal": self.cfg.thermal_stride}[self.mode]

    def should_process(self, frame_index: int) -> bool:
        if frame_index < self._next_frame:
            return False
        self._next_frame = frame_index + self.stride
        self.mode_frames[self.mode] += 1
        return True

    def update(self, t: float, n_detections: int) -> None:
        if n_detections:
            self._last_hit_t = t
        now = time.monotonic()
        if now - self._temp_checked > self.cfg.thermal_check_s:
            self._temp, self._temp_checked = self.read_temp(), now
        if self._temp is not None and self._temp >= self.cfg.thermal_limit_c:
            self.mode = "thermal"
        elif self._last_hit_t is not None and t - self._last_hit_t <= self.cfg.hold_active_s:
            self.mode = "active"
        else:
            self.mode = "search"

    def status(self) -> dict:
        return {"mode": self.mode, "stride": self.stride, "temp_c": self._temp,
                "frames_by_mode": dict(self.mode_frames)}
