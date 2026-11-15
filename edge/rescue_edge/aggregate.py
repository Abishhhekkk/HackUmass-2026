"""Turn per-frame located detections into de-duplicated people and a heat grid.

Two kinds of double counting are removed on the Jetson, before anything is sent:
1. Same person in many frames of one pass -> the tracker's track_id groups them.
2. Same person seen again on a later pass (drone circles back, track IDs differ) ->
   track summaries within ``merge_radius_m`` are merged, UNLESS the two tracks were
   visible at the same time (then they are provably different people).

Known limit: two people standing closer than merge_radius_m on different passes are
merged into one. Counts are "estimated people", never verified.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .geolocate import METHOD, latlon_to_offset, offset_to_latlon


@dataclass
class _Obs:
    t: float
    lat: float | None
    lon: float | None
    err: float | None
    conf: float
    reason: str | None


@dataclass
class TrackSummary:
    track_key: str
    first_seen: float
    last_seen: float
    n_frames: int
    confidence: float
    lat: float | None
    lon: float | None
    horizontal_error_m: float | None
    unlocated_reason: str | None = None


@dataclass
class Person:
    person_id: str
    lat: float
    lon: float
    horizontal_error_m: float
    confidence: float
    first_seen: float
    last_seen: float
    n_frames: int
    tracks: list[TrackSummary] = field(default_factory=list)
    cell_id: str = ""


@dataclass
class GridCell:
    cell_id: str
    row: int
    col: int
    count: int
    score: float
    person_ids: list[str]
    polygon: list[list[float]]  # GeoJSON ring [[lon, lat], ...]


@dataclass
class MissionState:
    people: list[Person]
    unlocated: list[TrackSummary]
    cells: list[GridCell]
    origin: tuple[float, float]
    cell_size_m: float
    frames_processed: int
    detections_processed: int
    data_start: float | None
    data_end: float | None
    location_failures: dict[str, int]


class Aggregator:
    def __init__(self, pass_id: str = "p1", min_frames: int = 3, merge_radius_m: float = 4.0,
                 cell_size_m: float = 10.0, recency_tau_s: float = 1800.0):
        self.pass_id = pass_id
        self.min_frames = min_frames
        self.merge_radius_m = merge_radius_m
        self.cell_size_m = cell_size_m
        self.recency_tau_s = recency_tau_s
        self.origin: tuple[float, float] | None = None
        self._obs: dict[str, list[_Obs]] = defaultdict(list)
        self.frames = 0
        self.detections = 0
        self.untracked = 0
        self.t_min: float | None = None
        self.t_max: float | None = None

    def new_pass(self, pass_id: str) -> None:
        """Track IDs restart (new video / tracker reset); keep them from colliding."""
        self.pass_id = pass_id

    def note_frame(self, t: float) -> None:
        self.frames += 1
        self.t_min = t if self.t_min is None else min(self.t_min, t)
        self.t_max = t if self.t_max is None else max(self.t_max, t)

    def add(self, t: float, track_id: int | None, conf: float, lat: float | None,
            lon: float | None, err: float | None, reason: str | None) -> None:
        self.detections += 1
        if track_id is None:
            self.untracked += 1  # tracker not confident yet; it will get an ID if it persists
            return
        if lat is not None and self.origin is None:
            self.origin = (lat, lon)
        self._obs[f"{self.pass_id}:{track_id}"].append(_Obs(t, lat, lon, err, conf, reason))

    # ------------------------------------------------------------------ finalize
    def _summaries(self) -> list[TrackSummary]:
        out = []
        for key, obs in self._obs.items():
            if len(obs) < self.min_frames:
                continue
            located = [o for o in obs if o.lat is not None]
            confs = sorted(o.conf for o in obs)
            conf = confs[len(confs) // 2]  # median confidence across the track
            ts = [o.t for o in obs]
            if located:
                out.append(TrackSummary(
                    key, min(ts), max(ts), len(obs), round(conf, 3),
                    statistics.median(o.lat for o in located),
                    statistics.median(o.lon for o in located),
                    round(statistics.median(o.err for o in located), 2),
                ))
            else:
                reason = Counter(o.reason for o in obs).most_common(1)[0][0]
                out.append(TrackSummary(key, min(ts), max(ts), len(obs), round(conf, 3),
                                        None, None, None, reason))
        return sorted(out, key=lambda s: (s.first_seen, s.track_key))

    def _dist_m(self, a_lat, a_lon, b_lat, b_lon) -> float:
        n, e = latlon_to_offset(a_lat, a_lon, b_lat, b_lon)
        return math.hypot(n, e)

    def _merge(self, tracks: list[TrackSummary]) -> list[list[TrackSummary]]:
        clusters: list[list[TrackSummary]] = []
        for tr in tracks:
            best, best_d = None, None
            for c in clusters:
                clat = statistics.fmean(x.lat for x in c)
                clon = statistics.fmean(x.lon for x in c)
                d = self._dist_m(clat, clon, tr.lat, tr.lon)
                overlap = any(tr.first_seen <= x.last_seen and x.first_seen <= tr.last_seen for x in c)
                if d <= self.merge_radius_m and not overlap and (best_d is None or d < best_d):
                    best, best_d = c, d
            if best is None:
                clusters.append([tr])
            else:
                best.append(tr)
        return clusters

    def _cell_of(self, lat: float, lon: float) -> tuple[int, int]:
        n, e = latlon_to_offset(self.origin[0], self.origin[1], lat, lon)
        return math.floor(n / self.cell_size_m), math.floor(e / self.cell_size_m)

    def _cell_polygon(self, row: int, col: int) -> list[list[float]]:
        s = self.cell_size_m
        corners = [(row * s, col * s), (row * s, (col + 1) * s), ((row + 1) * s, (col + 1) * s),
                   ((row + 1) * s, col * s), (row * s, col * s)]
        ring = []
        for n, e in corners:
            lat, lon = offset_to_latlon(self.origin[0], self.origin[1], n, e)
            ring.append([round(lon, 7), round(lat, 7)])
        return ring

    def finalize(self, now: float | None = None) -> MissionState:
        summaries = self._summaries()
        located = [s for s in summaries if s.lat is not None]
        unlocated = [s for s in summaries if s.lat is None]
        now = now if now is not None else (self.t_max or 0.0)

        people: list[Person] = []
        for i, cluster in enumerate(self._merge(located), start=1):
            # weight positions by 1/err^2 so the best-geolocated sighting dominates
            w = [1 / max(c.horizontal_error_m, 0.5) ** 2 for c in cluster]
            lat = sum(c.lat * wi for c, wi in zip(cluster, w)) / sum(w)
            lon = sum(c.lon * wi for c, wi in zip(cluster, w)) / sum(w)
            err = min(c.horizontal_error_m for c in cluster)
            people.append(Person(
                person_id=f"P{i:03d}", lat=lat, lon=lon, horizontal_error_m=round(err, 2),
                confidence=max(c.confidence for c in cluster),
                first_seen=min(c.first_seen for c in cluster),
                last_seen=max(c.last_seen for c in cluster),
                n_frames=sum(c.n_frames for c in cluster), tracks=cluster,
            ))

        cells: list[GridCell] = []
        if self.origin and people:
            by_cell: dict[tuple[int, int], list[Person]] = defaultdict(list)
            for p in people:
                rc = self._cell_of(p.lat, p.lon)
                p.cell_id = f"r{rc[0]}c{rc[1]}"
                by_cell[rc].append(p)
            for (r, c), ps in sorted(by_cell.items()):
                score = sum(p.confidence * math.exp(-max(0.0, now - p.last_seen) / self.recency_tau_s)
                            for p in ps)
                cells.append(GridCell(f"r{r}c{c}", r, c, len(ps), round(score, 3),
                                      [p.person_id for p in ps], self._cell_polygon(r, c)))

        failures = Counter(o.reason for obs in self._obs.values() for o in obs if o.reason)
        return MissionState(
            people=people, unlocated=unlocated, cells=cells,
            origin=self.origin or (0.0, 0.0), cell_size_m=self.cell_size_m,
            frames_processed=self.frames, detections_processed=self.detections,
            data_start=self.t_min, data_end=self.t_max, location_failures=dict(failures),
        )


HEAT_FORMULA = ("cell score = sum over estimated people in the cell of "
                "confidence * exp(-(snapshot_time - last_seen) / recency_tau_s)")
POSITION_METHOD = METHOD
