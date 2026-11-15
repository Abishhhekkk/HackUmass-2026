"""DJI ``.SRT`` flight logs -> telemetry samples aligned to the video's frames.

DJI drones record an ``.SRT`` subtitle file next to each video: one block per frame (or per
second on older models) with the drone's position. That is how existing drones, which have
no onboard AI or storage of their own, give RescueSight its telemetry. Supported styles
(field names vary by model; verify with a file from YOUR drone):

* Mini / Air / Mavic 3:  ``[latitude: 42.389012] [longitude: -72.528300] [rel_alt: 30.100 abs_alt: 120.500]``
* Enterprise / newer:    adds ``[gb_yaw: 91.0 gb_pitch: -90.0 gb_roll: 0.0]`` (gimbal attitude)
* Mavic 2:               ``[latitude : 42.38] [longtitude : -72.52] [altitude: 30.1]`` (sic)
* Phantom 3/4:           ``GPS(-72.5283,42.3890,19)`` with ``H 30.10m`` or ``BAROMETER:30.1``

Timing: each block's start time is its position in the video, so a sample's time is
``video_start_time + block start``, exactly the clock ``VideoFileSource`` stamps frames
with. No clock offset to guess. The wall-clock date in the block is the drone's LOCAL time;
it is only used (with ``utc_offset_hours``) when no ``video_start_time`` is configured.

Height: ``rel_alt`` / ``H`` / ``BAROMETER`` are height above the take-off point, used as
height above ground (flat-ground assumption). ``abs_alt`` (above sea level) is never used.

Heading (where the top of the image points):
1. ``gb_yaw`` (gimbal yaw) when present: this is the camera's own heading.
2. Otherwise the direction of travel between positions >= ``min_move_m`` apart, which is the
   nose direction only while the drone flies forward. Kept for up to ``cog_hold_s`` when the
   drone stops; after that the sample is ``heading_unknown`` (never guessed).

Tilt: with ``gb_pitch`` the camera's tilt from straight down is ``gb_pitch + 90`` (forward
tilt = positive pitch, as in geolocate.py); the 20 deg limit then rejects oblique frames.
Without gimbal fields the camera is assumed to point straight down (gimbal at -90).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .geolocate import latlon_to_offset
from .telemetry import TelemetryBuffer, TelemetrySample

_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->")
_NUM = r"(-?\d+(?:\.\d+)?)"
_FIELDS = {
    "lat": re.compile(r"\[\s*latitude\s*:\s*" + _NUM, re.I),
    "lon": re.compile(r"\[\s*long?t?itude\s*:\s*" + _NUM, re.I),  # longitude / longtitude
    "rel_alt": re.compile(r"rel_alt\s*:\s*" + _NUM, re.I),
    "altitude": re.compile(r"\[\s*altitude\s*:\s*" + _NUM, re.I),
    "gb_yaw": re.compile(r"gb_yaw\s*:\s*" + _NUM, re.I),
    "gb_pitch": re.compile(r"gb_pitch\s*:\s*" + _NUM, re.I),
    "gb_roll": re.compile(r"gb_roll\s*:\s*" + _NUM, re.I),
    "h": re.compile(r"(?:^|[\s,])H\s+" + _NUM + r"\s*m", re.M),
    "baro": re.compile(r"BAROMETER\s*:\s*" + _NUM, re.I),
}
_GPS = re.compile(r"GPS\s*\(\s*" + _NUM + r"\s*,\s*" + _NUM + r"(?:\s*,\s*" + _NUM + r")?\s*\)")
_DATETIME = re.compile(r"(\d{4})[-.](\d{2})[-.](\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?")


@dataclass
class SrtRecord:
    offset_s: float                 # position in the video
    lat: float | None = None
    lon: float | None = None
    alt_agl: float | None = None
    alt_source: str | None = None
    gb_yaw: float | None = None
    gb_pitch: float | None = None
    gb_roll: float | None = None
    local_time: datetime | None = None


@dataclass
class SrtStats:
    records: int = 0
    heading_from: dict = field(default_factory=lambda: {"gimbal": 0, "course": 0, "unknown": 0})
    alt_source: dict = field(default_factory=dict)
    no_gps: int = 0
    gimbal_tilt: bool = False
    start_time: float | None = None
    warnings: list = field(default_factory=list)


def parse_srt(text: str) -> list[SrtRecord]:
    records = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
        m = _TIME.search(block)
        if not m:
            continue
        hh, mm, ss, frac = m.groups()
        rec = SrtRecord(int(hh) * 3600 + int(mm) * 60 + int(ss) + int(frac) / 10 ** len(frac))
        body = re.sub(r"<[^>]+>", " ", block[m.end():])
        vals = {k: float(x.group(1)) for k, rx in _FIELDS.items() if (x := rx.search(body))}
        if "lat" in vals and "lon" in vals:
            rec.lat, rec.lon = vals["lat"], vals["lon"]
        elif g := _GPS.search(body):  # Phantom: GPS(lon, lat, satellites/alt)
            rec.lon, rec.lat = float(g.group(1)), float(g.group(2))
        for key, source in (("rel_alt", "rel_alt"), ("h", "H"), ("baro", "BAROMETER"),
                            ("altitude", "altitude (assumed above take-off)")):
            if key in vals:
                rec.alt_agl, rec.alt_source = vals[key], source
                break
        rec.gb_yaw, rec.gb_pitch, rec.gb_roll = vals.get("gb_yaw"), vals.get("gb_pitch"), vals.get("gb_roll")
        if d := _DATETIME.search(body):
            y, mo, da, h, mi, se, ms = d.groups()
            rec.local_time = datetime(int(y), int(mo), int(da), int(h), int(mi), int(se),
                                      int((ms or "0").ljust(3, "0")) * 1000)
        records.append(rec)
    return records


def _bearing_deg(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    north, east = latlon_to_offset(lat0, lon0, lat1, lon1)
    return math.degrees(math.atan2(east, north)) % 360


def srt_samples(records: list[SrtRecord], start_time: float, min_move_m: float = 2.0,
                cog_hold_s: float = 3.0, stats: SrtStats | None = None) -> list[TelemetrySample]:
    stats = stats if stats is not None else SrtStats()
    stats.records = len(records)
    out = []
    anchor = None                         # last position used for the course
    course, course_t = None, None
    for rec in records:
        t = start_time + rec.offset_s
        has_gps = (rec.lat is not None and rec.lon is not None
                   and not (abs(rec.lat) < 1e-6 and abs(rec.lon) < 1e-6))  # DJI writes 0,0 without GPS
        stats.no_gps += not has_gps
        if rec.alt_source:
            stats.alt_source[rec.alt_source] = stats.alt_source.get(rec.alt_source, 0) + 1

        hdg, heading_valid = 0.0, False
        if rec.gb_yaw is not None:
            hdg, heading_valid = rec.gb_yaw % 360, True
            stats.heading_from["gimbal"] += 1
        else:
            if has_gps:
                if anchor is None:
                    anchor = (rec.lat, rec.lon)
                else:
                    north, east = latlon_to_offset(anchor[0], anchor[1], rec.lat, rec.lon)
                    if math.hypot(north, east) >= min_move_m:
                        course, course_t = _bearing_deg(anchor[0], anchor[1], rec.lat, rec.lon), t
                        anchor = (rec.lat, rec.lon)
            if course is not None and t - course_t <= cog_hold_s:
                hdg, heading_valid = course, True
                stats.heading_from["course"] += 1
            else:
                stats.heading_from["unknown"] += 1

        pitch = roll = 0.0
        if rec.gb_pitch is not None:
            stats.gimbal_tilt = True
            pitch = rec.gb_pitch + 90.0          # tilt from straight down; forward = positive
            roll = rec.gb_roll or 0.0
        out.append(TelemetrySample(
            t=t, lat=rec.lat if has_gps else 0.0, lon=rec.lon if has_gps else 0.0,
            alt_agl=rec.alt_agl if rec.alt_agl is not None else -1.0,
            hdg=hdg, pitch=pitch, roll=roll, fix=3 if has_gps else 0, hdop=1.0,
            heading_valid=heading_valid))
    if not stats.gimbal_tilt:
        stats.warnings.append("no gimbal pitch in this SRT: the camera is ASSUMED to point straight "
                              "down (gimbal -90). Footage with the camera tilted will be mislocated.")
    if stats.heading_from["course"]:
        stats.warnings.append("heading taken from the direction of travel: correct only while the "
                              "drone flies nose-first. Sideways flight or turning on the spot breaks it.")
    if any(src.startswith("altitude") for src in stats.alt_source):
        stats.warnings.append("height from an 'altitude' field, assumed to be above take-off; "
                              "check your model's SRT documentation.")
    return out


def start_from_srt(records: list[SrtRecord], utc_offset_hours: float) -> float | None:
    """Video start as UTC epoch from the first block's local wall-clock time."""
    for rec in records:
        if rec.local_time is not None:
            local = rec.local_time - timedelta(seconds=rec.offset_s)
            return (local - timedelta(hours=utc_offset_hours)).replace(tzinfo=timezone.utc).timestamp()
    return None


def load_srt(path: str | Path, buf: TelemetryBuffer | None = None, start_time: float | None = None,
             utc_offset_hours: float = 0.0, min_move_m: float = 2.0,
             cog_hold_s: float = 3.0) -> tuple[TelemetryBuffer, SrtStats]:
    """Parse an SRT into ``buf``. ``start_time`` = UTC epoch of the video's first frame (the
    same value as source.video_start_time); derived from the SRT when None."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    records = parse_srt(text)
    if not records:
        raise ValueError(f"no DJI telemetry blocks found in {path}")
    if start_time is None:
        start_time = start_from_srt(records, utc_offset_hours)
        if start_time is None:
            raise ValueError(f"{path} has no date/time; set source.video_start_time")
    stats = SrtStats(start_time=start_time)
    buf = buf if buf is not None else TelemetryBuffer(max_samples=None)
    for sample in srt_samples(records, start_time, min_move_m, cog_hold_s, stats):
        buf.add(sample)
    return buf, stats
