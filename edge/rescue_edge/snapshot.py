"""Versioned, checksummed result packages ("snapshots").

Each snapshot is the COMPLETE current state of the mission, never an increment:

  <root>/<mission_id>/v000007/
      sightings.csv      one row per estimated person + rows for unlocated tracks
      heatmap.geojson    grid cells (Polygon) + people (Point) for the map
      telemetry.json     latest frame-aligned drone telemetry fix, or its unavailable reason
      report.md          deterministic text report
      manifest.json      written LAST; lists every file with sha256 + size
  <root>/<mission_id>/LATEST   -> "7"

A version directory is built under a temp name and renamed into place, so a crash or
power loss never leaves a half-written snapshot. A new version is only written when
the data actually changed.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import time
from pathlib import Path

from .aggregate import HEAT_FORMULA, POSITION_METHOD, MissionState
from .report import build_report, error_stats, iso

SCHEMA_VERSION = 1
DISCLAIMER = ("Estimated sightings for human review. Positions are approximate "
              "(see horizontal_error_m); counts are not verified people.")

CSV_FIELDS = ["person_id", "status", "lat", "lon", "horizontal_error_m", "confidence",
              "first_seen_utc", "last_seen_utc", "n_frames", "n_tracks", "cell_id",
              "position_method", "unlocated_reason", "track_ids"]


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def render_csv(state: MissionState) -> bytes:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS, lineterminator="\n")
    w.writeheader()
    for p in state.people:
        w.writerow({
            "person_id": p.person_id, "status": "estimated_sighting",
            "lat": f"{p.lat:.7f}", "lon": f"{p.lon:.7f}",
            "horizontal_error_m": p.horizontal_error_m, "confidence": p.confidence,
            "first_seen_utc": iso(p.first_seen), "last_seen_utc": iso(p.last_seen),
            "n_frames": p.n_frames, "n_tracks": len(p.tracks), "cell_id": p.cell_id,
            "position_method": POSITION_METHOD, "unlocated_reason": "",
            "track_ids": ";".join(t.track_key for t in p.tracks),
        })
    for i, t in enumerate(state.unlocated, start=1):
        w.writerow({
            "person_id": f"U{i:03d}", "status": "unlocated_sighting", "lat": "", "lon": "",
            "horizontal_error_m": "", "confidence": t.confidence,
            "first_seen_utc": iso(t.first_seen), "last_seen_utc": iso(t.last_seen),
            "n_frames": t.n_frames, "n_tracks": 1, "cell_id": "", "position_method": "",
            "unlocated_reason": t.unlocated_reason, "track_ids": t.track_key,
        })
    return buf.getvalue().encode()


def render_geojson(state: MissionState) -> bytes:
    feats = []
    for c in state.cells:
        feats.append({"type": "Feature",
                      "geometry": {"type": "Polygon", "coordinates": [c.polygon]},
                      "properties": {"kind": "cell", "cell_id": c.cell_id, "count": c.count,
                                     "score": c.score, "person_ids": c.person_ids}})
    for p in state.people:
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [round(p.lon, 7), round(p.lat, 7)]},
                      "properties": {"kind": "person", "person_id": p.person_id,
                                     "confidence": p.confidence,
                                     "horizontal_error_m": p.horizontal_error_m,
                                     "first_seen_utc": iso(p.first_seen),
                                     "last_seen_utc": iso(p.last_seen),
                                     "n_tracks": len(p.tracks), "cell_id": p.cell_id,
                                     "status": "estimated_sighting"}})
    fc = {"type": "FeatureCollection", "features": feats,
          "properties": {"cell_size_m": state.cell_size_m,
                         "origin": {"lat": state.origin[0], "lon": state.origin[1]},
                         "heat_formula": HEAT_FORMULA}}
    return json.dumps(fc, separators=(",", ":")).encode()


def render_telemetry(telemetry: dict | None) -> bytes:
    return json.dumps(
        telemetry or {"schema_version": 1, "available": False, "reason": "no_telemetry"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


class SnapshotWriter:
    def __init__(self, root: str | Path, mission_id: str, drone_id: str):
        self.dir = Path(root) / mission_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.mission_id, self.drone_id = mission_id, drone_id
        self.version = latest_version(self.dir)
        self._last_hash = None
        if self.version:
            m = json.loads((self.dir / f"v{self.version:06d}" / "manifest.json").read_text())
            self._last_hash = m.get("content_hash")

    def write_if_changed(self, state: MissionState, force: bool = False,
                         telemetry: dict | None = None) -> int | None:
        files = {
            "sightings.csv": render_csv(state),
            "heatmap.geojson": render_geojson(state),
            "telemetry.json": render_telemetry(telemetry),
        }
        content_hash = sha256_bytes(b"".join(files[k] for k in sorted(files)))
        if content_hash == self._last_hash and not force:
            return None
        version = self.version + 1
        files["report.md"] = build_report(state, self.mission_id, self.drone_id, version).encode()
        med, p90 = error_stats(state)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "mission_id": self.mission_id,
            "drone_id": self.drone_id,
            "version": version,
            "created_at": iso(time.time()),
            "content_hash": content_hash,
            "data_window": {"start": iso(state.data_start), "end": iso(state.data_end)},
            "origin": {"lat": state.origin[0], "lon": state.origin[1]},
            "cell_size_m": state.cell_size_m,
            "position_method": POSITION_METHOD,
            "heat_formula": HEAT_FORMULA,
            "summary": {
                "estimated_people": len(state.people),
                "unlocated_tracks": len(state.unlocated),
                "occupied_cells": len(state.cells),
                "max_cell_count": max((c.count for c in state.cells), default=0),
                "frames_processed": state.frames_processed,
                "detections_processed": state.detections_processed,
                "median_error_m": med, "p90_error_m": p90,
                "location_failures": state.location_failures,
            },
            "files": [{"name": n, "sha256": sha256_bytes(b), "bytes": len(b)}
                      for n, b in sorted(files.items())],
            "status": "estimated",
            "disclaimer": DISCLAIMER,
        }
        tmp = self.dir / f".v{version:06d}.tmp"
        final = self.dir / f"v{version:06d}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        for name, data in files.items():
            _write_durable(tmp / name, data)
        _write_durable(tmp / "manifest.json", json.dumps(manifest, indent=2).encode())
        os.replace(tmp, final)
        _write_durable(self.dir / "LATEST.tmp", str(version).encode())
        os.replace(self.dir / "LATEST.tmp", self.dir / "LATEST")
        self.version, self._last_hash = version, content_hash
        return version


def _write_durable(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def latest_version(mission_dir: Path) -> int:
    p = Path(mission_dir) / "LATEST"
    if p.exists():
        try:
            v = int(p.read_text().strip())
            if (Path(mission_dir) / f"v{v:06d}" / "manifest.json").exists():
                return v
        except ValueError:
            pass
    # fall back to scanning (e.g. crash between rename and LATEST update)
    vs = [int(d.name[1:]) for d in Path(mission_dir).glob("v[0-9]*") if (d / "manifest.json").exists()]
    return max(vs, default=0)


def snapshot_dir(root: str | Path, mission_id: str, version: int) -> Path:
    return Path(root) / mission_id / f"v{version:06d}"
