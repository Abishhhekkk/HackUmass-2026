"""Drone pose telemetry: replay from CSV, or live JSON lines from the ESP32 over serial.

ESP32 line protocol (one JSON object per line, 5-10 Hz), see firmware/esp32_telemetry:
  {"t": 1763130128.120, "lat": 42.3901, "lon": -72.5202, "alt_agl": 24.0,
   "hdg": 91.0, "pitch": 0.8, "roll": -1.2, "fix": 3, "sats": 11, "hdop": 0.9}

``t`` is GPS-derived UTC epoch seconds. The Jetson has no RTC battery, so the live
reader also tracks the offset between GPS time and the Jetson clock; frames are
timestamped with ``local_time + offset`` so they line up with telemetry.
"""
from __future__ import annotations

import bisect
import csv
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .geolocate import Pose


@dataclass(frozen=True)
class TelemetrySample:
    t: float
    lat: float
    lon: float
    alt_agl: float
    hdg: float
    pitch: float
    roll: float
    fix: int = 3          # 0/1 = no fix, 2 = 2D, 3 = 3D
    hdop: float = 1.0

    @property
    def valid(self) -> bool:
        return self.fix >= 3 and self.hdop <= 5.0 and self.alt_agl > 0

    @classmethod
    def from_dict(cls, d: dict) -> "TelemetrySample":
        return cls(
            t=float(d["t"]), lat=float(d["lat"]), lon=float(d["lon"]),
            alt_agl=float(d["alt_agl"]), hdg=float(d["hdg"]),
            pitch=float(d.get("pitch", 0.0)), roll=float(d.get("roll", 0.0)),
            fix=int(d.get("fix", 3)), hdop=float(d.get("hdop", 1.0)),
        )


@dataclass(frozen=True)
class PoseLookup:
    pose: Pose | None
    reason: str | None = None  # why pose is None


def _lerp_angle(a: float, b: float, w: float) -> float:
    d = ((b - a + 180) % 360) - 180
    return (a + w * d) % 360


class TelemetryBuffer:
    """Time-ordered samples with interpolation. Thread-safe for one writer, many readers."""

    def __init__(self, max_gap_s: float = 1.0, max_age_s: float = 2.0):
        self.max_gap_s = max_gap_s    # don't interpolate across gaps bigger than this
        self.max_age_s = max_age_s    # nearest sample must be at most this far from frame time
        self._t: list[float] = []
        self._s: list[TelemetrySample] = []
        self._lock = threading.Lock()

    def add(self, s: TelemetrySample) -> None:
        with self._lock:
            i = bisect.bisect_right(self._t, s.t)
            self._t.insert(i, s.t)
            self._s.insert(i, s)

    def __len__(self) -> int:
        return len(self._t)

    @property
    def first_valid(self) -> TelemetrySample | None:
        return next((s for s in self._s if s.valid), None)

    def at(self, t: float) -> PoseLookup:
        with self._lock:
            if not self._t:
                return PoseLookup(None, "no_telemetry")
            i = bisect.bisect_left(self._t, t)
            before = self._s[i - 1] if i > 0 else None
            after = self._s[i] if i < len(self._s) else None
        if after is not None and after.t == t:
            before = after
        candidates = [s for s in (before, after) if s is not None]
        nearest = min(candidates, key=lambda s: abs(s.t - t))
        if abs(nearest.t - t) > self.max_age_s:
            return PoseLookup(None, "telemetry_stale")
        if not nearest.valid:
            return PoseLookup(None, "gnss_fix_invalid")
        if before and after and before is not after and before.valid and after.valid \
                and (after.t - before.t) <= self.max_gap_s:
            w = (t - before.t) / (after.t - before.t)
            s = TelemetrySample(
                t=t,
                lat=before.lat + w * (after.lat - before.lat),
                lon=before.lon + w * (after.lon - before.lon),
                alt_agl=before.alt_agl + w * (after.alt_agl - before.alt_agl),
                hdg=_lerp_angle(before.hdg, after.hdg, w),
                pitch=before.pitch + w * (after.pitch - before.pitch),
                roll=before.roll + w * (after.roll - before.roll),
            )
        else:
            s = nearest
        return PoseLookup(Pose(s.lat, s.lon, s.alt_agl, s.hdg, s.pitch, s.roll))


def load_csv(path: str | Path, buf: TelemetryBuffer | None = None) -> TelemetryBuffer:
    buf = buf if buf is not None else TelemetryBuffer()
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            buf.add(TelemetrySample.from_dict(row))
    return buf


class SerialTelemetryReader(threading.Thread):
    """Reads ESP32 JSON lines into a TelemetryBuffer and estimates GPS-vs-local clock offset."""

    def __init__(self, port: str, baud: int, buf: TelemetryBuffer, log_path: str | None = None):
        super().__init__(daemon=True)
        self.port, self.baud, self.buf = port, baud, buf
        self.log_path = log_path
        self.clock_offset_s: float | None = None  # gps_time - local_time
        self.last_error: str | None = None
        self._stop = threading.Event()

    def now(self) -> float:
        """Current time on the GPS clock (falls back to local clock before first fix)."""
        return time.time() + (self.clock_offset_s or 0.0)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import serial  # pyserial, only needed for live flights

        log = open(self.log_path, "a", newline="") if self.log_path else None
        writer = None
        while not self._stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as ser:
                    while not self._stop.is_set():
                        line = ser.readline().decode("utf-8", "ignore").strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            d = json.loads(line)
                            s = TelemetrySample.from_dict(d)
                        except (ValueError, KeyError):
                            continue
                        if s.valid:
                            # smoothed estimate; serial latency is a few ms, fine for this use
                            off = s.t - time.time()
                            self.clock_offset_s = off if self.clock_offset_s is None \
                                else 0.9 * self.clock_offset_s + 0.1 * off
                        self.buf.add(s)
                        if log:
                            if writer is None:
                                writer = csv.DictWriter(log, fieldnames=list(s.__dict__.keys()))
                                if log.tell() == 0:
                                    writer.writeheader()
                            writer.writerow(s.__dict__)
                            log.flush()
            except Exception as e:  # serial unplugged etc. -> retry
                self.last_error = str(e)
                time.sleep(1.0)
