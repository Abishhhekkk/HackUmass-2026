#!/usr/bin/env python3
"""SIMULATED flight log for an aerial clip that has no real one (no DJI .SRT), plus a config.

    python scripts/simulate_telemetry.py data/footage/crowd.mp4
    cd edge && python -m rescue_edge.main --config ../config/crowd.sim.yaml --serve

Detections are real (the clip is real); POSITIONS ARE A DEMO: they are computed from a made-up
flight path, so they are only meaningful relative to that path, not on a real map. The mission
id starts with "sim-", and every snapshot's telemetry.json records the source as simulated.

Flight path (pick to roughly match what the clip shows):
* straight: from --lat/--lon, flying --heading degrees (0 = north, 90 = east) at --speed m/s
* hover:    fixed at --lat/--lon, facing --heading
Height --alt m above flat ground; camera assumed straight down, image top = direction of flight.
Choose top-down (nadir) clips: oblique footage breaks the geometry the positions rely on.

Writes data/footage/<clip>_sim_telemetry.csv and config/<clip>.sim.yaml (from
config/edge.footage.yaml, with the camera size read from the video).
"""
from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "edge"))
from rescue_edge.geolocate import offset_to_latlon  # noqa: E402

LABEL = "SIMULATED flight path (not real GPS)"


def video_info(path: Path) -> tuple[float, int, int, int]:
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise SystemExit(f"cannot open video {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if frames <= 0 or w <= 0 or h <= 0:
        raise SystemExit(f"could not read frame count/size from {path}")
    return fps, frames, w, h


def flight_rows(start: float, duration: float, lat: float, lon: float, alt: float, heading: float,
                speed: float, pattern: str, hz: float) -> list[list]:
    rows = []
    n = int(math.ceil((duration + 2.0) * hz)) + 1   # 1 s margin either side
    for i in range(n):
        dt = -1.0 + i / hz
        dist = max(dt, 0.0) * speed if pattern == "straight" else 0.0
        north = dist * math.cos(math.radians(heading))
        east = dist * math.sin(math.radians(heading))
        la, lo = offset_to_latlon(lat, lon, north, east)
        rows.append([f"{start + dt:.3f}", f"{la:.8f}", f"{lo:.8f}", f"{alt:.2f}",
                     f"{heading % 360:.1f}", "0.0", "0.0", 3, 1.0, 1])
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("--pattern", choices=("straight", "hover"), default="straight")
    ap.add_argument("--lat", type=float, default=42.38925, help="start latitude (default: a UMass field)")
    ap.add_argument("--lon", type=float, default=-72.52830)
    ap.add_argument("--alt", type=float, default=40.0, help="height above ground, m")
    ap.add_argument("--heading", type=float, default=90.0, help="deg clockwise from north")
    ap.add_argument("--speed", type=float, default=5.0, help="m/s (straight)")
    ap.add_argument("--dfov", type=float, default=82.1, help="camera diagonal field of view, deg")
    ap.add_argument("--start", help="UTC time of the first frame, ISO-8601 (default: now)")
    ap.add_argument("--hz", type=float, default=10.0)
    ap.add_argument("--base-config", type=Path, default=REPO / "config" / "edge.footage.yaml")
    ap.add_argument("--out-csv", type=Path)
    ap.add_argument("--config-out", type=Path)
    args = ap.parse_args()

    if args.alt <= 0 or args.speed < 0 or args.hz <= 0 or not 0 < args.dfov < 180:
        raise SystemExit("--alt and --hz must be positive, --speed >= 0, --dfov in (0, 180)")
    video = args.video.resolve()
    fps, frames, w, h = video_info(video)
    duration = frames / fps
    start = (datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp() if args.start
             else float(int(datetime.now(timezone.utc).timestamp())))
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", video.stem)[:60]
    out_csv = (args.out_csv or REPO / "data" / "footage" / f"{stem}_sim_telemetry.csv").resolve()
    config_out = (args.config_out or REPO / "config" / f"{stem}.sim.yaml").resolve()
    if config_out.parent.name != "config":
        raise SystemExit("--config-out must be inside a config/ folder (relative paths are resolved from its parent)")

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop", "heading_valid"])
        cw.writerows(flight_rows(start, duration, args.lat, args.lon, args.alt, args.heading,
                                 args.speed, args.pattern, args.hz))

    cfg = yaml.safe_load(args.base_config.read_text())
    cfg["mission_id"] = f"sim-{stem}"[:80]
    cfg["drone_id"] = "drone-sim"
    cfg["source"] = {"kind": "video", "path": str(video), "realtime": True,
                     "video_start_time": datetime.fromtimestamp(start, tz=timezone.utc)
                     .isoformat().replace("+00:00", "Z")}
    cfg["telemetry"] = {"kind": "replay", "path": str(out_csv), "max_gap_s": 1.0, "max_age_s": 2.0,
                        "source_label": LABEL}
    cfg["camera"] = {"width": w, "height": h, "dfov_deg": args.dfov}
    config_out.parent.mkdir(parents=True, exist_ok=True)
    header = (f"# GENERATED by scripts/simulate_telemetry.py for {video.name}.\n"
              f"# {LABEL}: {args.pattern}, {args.alt:g} m up, heading {args.heading:g} deg"
              + (f", {args.speed:g} m/s" if args.pattern == "straight" else "") +
              f", from {args.lat:.5f}, {args.lon:.5f}.\n"
              "# Detections are real; positions are only meaningful relative to this made-up path.\n")
    config_out.write_text(header + yaml.safe_dump(cfg, sort_keys=False))

    travel = args.speed * duration if args.pattern == "straight" else 0.0
    print(f"{video.name}: {w}x{h}, {fps:.2f} fps, {frames} frames ({duration:.1f} s)")
    print(f"simulated {args.pattern} flight: {args.alt:g} m up, heading {args.heading:g} deg, "
          f"{travel:.0f} m travelled")
    print(f"telemetry: {out_csv}")
    print(f"config:    {config_out}")
    print(f"run:       cd edge && python -m rescue_edge.main --config ../config/{config_out.name} --serve")
    print("Positions are a DEMO (simulated flight path); the mission id starts with 'sim-'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
