#!/usr/bin/env python3
"""Inspect a DJI .SRT flight log, and optionally convert it to Vantage telemetry CSV.

    python scripts/srt_to_telemetry.py data/footage/DJI_0001.SRT --utc-offset -5
    python scripts/srt_to_telemetry.py DJI_0001.SRT --start 2026-11-14T15:00:00Z --csv tel.csv

Run this on a real file from YOUR drone first: it prints what was found (positions, height
source, heading source, gimbal tilt) and any warnings. The pipeline reads .SRT directly
(config/edge.footage.yaml, telemetry.kind: srt); the CSV is for replay/inspection.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
from rescue_edge.srt import load_srt  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("srt", type=Path)
    ap.add_argument("--start", help="UTC time of the video's first frame (ISO-8601); default: "
                                    "from the SRT's local time and --utc-offset")
    ap.add_argument("--utc-offset", type=float, default=0.0,
                    help="hours the drone's clock is ahead of UTC (Amherst in November: -5)")
    ap.add_argument("--csv", type=Path, help="write telemetry CSV here")
    args = ap.parse_args()

    start = (datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp()
             if args.start else None)
    buf, stats = load_srt(args.srt, start_time=start, utc_offset_hours=args.utc_offset)
    samples = list(buf._s)
    valid = [s for s in samples if s.valid]
    reasons: dict[str, int] = {}
    for s in samples:
        if not s.valid:
            reasons[s.invalid_reason] = reasons.get(s.invalid_reason, 0) + 1
    t0 = datetime.fromtimestamp(stats.start_time, tz=timezone.utc).isoformat()
    print(f"{args.srt.name}: {stats.records} records over {samples[-1].t - samples[0].t:.1f} s")
    print(f"video_start_time (UTC): {t0}   <- put this in source.video_start_time")
    print(f"usable samples: {len(valid)}/{len(samples)}" + (f"; unusable: {reasons}" if reasons else ""))
    print(f"heading from: {stats.heading_from}; height from: {stats.alt_source}; "
          f"gimbal tilt logged: {stats.gimbal_tilt}")
    if valid:
        alts = sorted(s.alt_agl for s in valid)
        print(f"height above take-off: {alts[0]:.1f} to {alts[-1]:.1f} m (median {alts[len(alts) // 2]:.1f})")
    for w in stats.warnings:
        print(f"WARNING: {w}")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop", "heading_valid"])
            for s in samples:
                w.writerow([f"{s.t:.3f}", f"{s.lat:.7f}", f"{s.lon:.7f}", f"{s.alt_agl:.2f}",
                            f"{s.hdg:.1f}", f"{s.pitch:.1f}", f"{s.roll:.1f}", s.fix, s.hdop,
                            int(s.heading_valid)])
        print(f"wrote {args.csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
