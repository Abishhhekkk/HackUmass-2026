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
import logging
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .geolocate import Pose

log = logging.getLogger(__name__)


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
    def invalid_reason(self) -> str | None:
        values = (self.t, self.lat, self.lon, self.alt_agl, self.hdg, self.pitch, self.roll, self.hdop)
        if (
            not all(math.isfinite(value) for value in values)
            or not (-90 <= self.lat <= 90 and -180 <= self.lon <= 180)
            or self.fix < 3
            or not 0 <= self.hdop <= 5.0
        ):
            return "gnss_fix_invalid"
        if self.alt_agl <= 0:
            return "altitude_invalid"
        return None

    @property
    def valid(self) -> bool:
        return self.invalid_reason is None

    @classmethod
    def from_dict(cls, d: dict) -> "TelemetrySample":
        try:
            sample = cls(
                t=float(d["t"]), lat=float(d["lat"]), lon=float(d["lon"]),
                alt_agl=float(d["alt_agl"]), hdg=float(d["hdg"]),
                pitch=float(d.get("pitch", 0.0)), roll=float(d.get("roll", 0.0)),
                fix=int(d.get("fix", 3)), hdop=float(d.get("hdop", 1.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Telemetry line is missing or has invalid required numeric fields") from exc
        if not all(math.isfinite(value) for value in (
            sample.t, sample.lat, sample.lon, sample.alt_agl, sample.hdg,
            sample.pitch, sample.roll, sample.hdop,
        )):
            raise ValueError("Telemetry values must be finite numbers")
        if not (-90 <= sample.lat <= 90 and -180 <= sample.lon <= 180):
            raise ValueError("Telemetry latitude/longitude are outside WGS84 bounds")
        return sample


@dataclass(frozen=True)
class PoseLookup:
    pose: Pose | None
    reason: str | None = None  # why pose is None


def _lerp_angle(a: float, b: float, w: float) -> float:
    d = ((b - a + 180) % 360) - 180
    return (a + w * d) % 360


class TelemetryBuffer:
    """Time-ordered samples with interpolation. Thread-safe for one writer, many readers.

    ``max_samples`` bounds memory for a live serial feed (oldest samples are dropped).
    Use ``None`` for a replayed log: the whole flight is loaded before the first frame,
    so a cap would discard the start of the flight and every early frame would be stale.
    """

    def __init__(self, max_gap_s: float = 1.0, max_age_s: float = 2.0, max_samples: int | None = 512):
        if max_samples is not None and max_samples < 2:
            raise ValueError("max_samples must be at least 2")
        self.max_gap_s = max_gap_s    # don't interpolate across gaps bigger than this
        self.max_age_s = max_age_s    # nearest sample must be at most this far from frame time
        self.max_samples = max_samples
        self._t: list[float] = []
        self._s: list[TelemetrySample] = []
        self._lock = threading.Lock()

    def add(self, s: TelemetrySample) -> None:
        with self._lock:
            i = bisect.bisect_right(self._t, s.t)
            self._t.insert(i, s.t)
            self._s.insert(i, s)
            excess = 0 if self.max_samples is None else len(self._s) - self.max_samples
            if excess > 0:
                del self._t[:excess]
                del self._s[:excess]

    def __len__(self) -> int:
        with self._lock:
            return len(self._t)

    @property
    def first_valid(self) -> TelemetrySample | None:
        with self._lock:
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
            return PoseLookup(None, nearest.invalid_reason)
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
    buf = buf if buf is not None else TelemetryBuffer(max_samples=None)
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
        self._stop_event = threading.Event()
        self._serial_lock = threading.Lock()
        self._serial = None

    def now(self) -> float:
        """Current time on the GPS clock (falls back to local clock before first fix)."""
        return time.time() + (self.clock_offset_s or 0.0)

    def stop(self) -> None:
        self._stop_event.set()
        with self._serial_lock:
            if self._serial is not None and self._serial.is_open:
                self._serial.close()

    def run(self) -> None:
        try:
            import serial  # pyserial, only needed for live flights
        except ImportError as exc:
            self.last_error = "pyserial is not installed; install edge/requirements.txt"
            log.error(self.last_error)
            return

        log_file = None
        try:
            if self.log_path:
                path = Path(self.log_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                log_file = path.open("a", newline="")
        except OSError as exc:
            self.last_error = f"cannot open telemetry log {self.log_path}: {exc}"
            log.error(self.last_error)
            return

        writer = None
        try:
            while not self._stop_event.is_set():
                ser = None
                try:
                    ser = serial.Serial(self.port, self.baud, timeout=0.5)
                    with self._serial_lock:
                        self._serial = ser
                    self.last_error = None
                    log.info("ESP32 serial telemetry connected on %s at %d baud", self.port, self.baud)
                    while not self._stop_event.is_set():
                        line = ser.readline().decode("utf-8", "ignore").strip()
                        if not line.startswith("{"):
                            continue
                        try:
                            d = json.loads(line)
                            s = TelemetrySample.from_dict(d)
                        except (ValueError, KeyError, TypeError) as exc:
                            log.debug("discarding invalid ESP32 telemetry line: %s", exc)
                            continue
                        if s.valid:
                            # GPS time anchors camera timestamps even when the Jetson has no RTC.
                            off = s.t - time.time()
                            self.clock_offset_s = off if self.clock_offset_s is None \
                                else 0.9 * self.clock_offset_s + 0.1 * off
                        self.buf.add(s)
                        if log_file:
                            if writer is None:
                                writer = csv.DictWriter(log_file, fieldnames=list(s.__dict__.keys()))
                                if log_file.tell() == 0:
                                    writer.writeheader()
                            writer.writerow(s.__dict__)
                            log_file.flush()
                except (serial.SerialException, OSError) as exc:
                    if not self._stop_event.is_set():
                        self.last_error = str(exc)
                        log.warning("ESP32 serial telemetry unavailable on %s: %s; retrying", self.port, exc)
                        self._stop_event.wait(1.0)
                finally:
                    with self._serial_lock:
                        if self._serial is ser:
                            self._serial = None
                    if ser is not None and ser.is_open:
                        ser.close()
        finally:
            if log_file:
                log_file.close()


FLIGHT_PATH_FIELDS = ["observed_at", "t", "lat", "lon", "alt_agl_m", "heading_deg", "pitch_deg",
                      "roll_deg", "valid", "reason"]


class FlightPathRecorder:
    """Downsampled, frame-aligned drone pose history for the snapshot's flight_path.csv.

    One row at most every ``interval_s`` of flight time. Invalid/stale fixes are recorded as
    rows with valid=0 and the reason (a gap in the track), never with a guessed position.
    ~70 bytes per row: a one-hour mission at 1 Hz is about 250 KB.
    """

    def __init__(self, interval_s: float = 1.0):
        if interval_s <= 0:
            raise ValueError("flight path interval must be positive")
        self.interval_s = interval_s
        self._rows: list[dict] = []
        self._last_t: float | None = None

    def due(self, t: float) -> bool:
        return self._last_t is None or t - self._last_t >= self.interval_s

    def record(self, t: float, lookup: PoseLookup) -> None:
        from .report import iso

        self._last_t = t
        p = lookup.pose
        self._rows.append({
            "observed_at": iso(t), "t": f"{t:.3f}",
            "lat": f"{p.lat:.7f}" if p else "", "lon": f"{p.lon:.7f}" if p else "",
            "alt_agl_m": f"{p.altitude_agl_m:.2f}" if p else "",
            "heading_deg": f"{p.heading_deg:.1f}" if p else "",
            "pitch_deg": f"{p.pitch_deg:.1f}" if p else "",
            "roll_deg": f"{p.roll_deg:.1f}" if p else "",
            "valid": 1 if p else 0, "reason": "" if p else (lookup.reason or "no_telemetry"),
        })

    def rows(self) -> list[dict]:
        return list(self._rows)
