#!/usr/bin/env python3
"""Generate a synthetic mission: a lawnmower flight over a field with known people.

Writes (default data/demo/):
  telemetry.csv     10 Hz drone pose with GPS/attitude noise and a GPS dropout
  detections.jsonl  per-frame person boxes with tracker-style IDs (incl. ID switches,
                    misses and false positives), consumed by ReplaySource + MockDetector
  ground_truth.csv  true person positions, for scripts/evaluate.py

Boxes are produced by projecting each person's TRUE position with the TRUE pose, while
the telemetry file contains the NOISY pose, so geolocation error is realistic.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
from rescue_edge.geolocate import CameraModel, Pose, ground_to_pixel, offset_to_latlon  # noqa: E402

ORIGIN = (42.38925, -72.52830)       # an open field near UMass Amherst (demo only)
T0 = 1_794_668_400.0                  # 2026-11-14T15:00:00Z


def people_layout(rng: random.Random) -> list[tuple[str, float, float]]:
    """(id, north_m, east_m). Three clusters plus scattered individuals."""
    out = []
    clusters = [(25, 30, 7), (70, 95, 5), (55, 150, 4)]
    for ci, (n, e, k) in enumerate(clusters):
        for j in range(k):
            out.append((f"T{len(out)+1:03d}", n + rng.uniform(-6, 6), e + rng.uniform(-6, 6)))
    for _ in range(6):
        out.append((f"T{len(out)+1:03d}", rng.uniform(5, 95), rng.uniform(10, 190)))
    # enforce >= 2 m spacing so ground truth is unambiguous
    kept = []
    for p in out:
        if all(math.hypot(p[1] - q[1], p[2] - q[2]) >= 2.0 for q in kept):
            kept.append(p)
    return kept


def flight_path(alt: float, speed: float, hz: float):
    """Yield (t, north, east, heading) along east-west lanes 30 m apart."""
    lanes = [0, 30, 60, 90, 105]
    t, dt = 0.0, 1 / hz
    for li, n in enumerate(lanes):
        eastbound = li % 2 == 0
        e0, e1 = (-10, 210) if eastbound else (210, -10)
        heading = 90.0 if eastbound else 270.0
        length = abs(e1 - e0)
        steps = int(length / speed * hz)
        for s in range(steps):
            e = e0 + (e1 - e0) * s / steps
            yield t, n, e, heading
            t += dt
        if li + 1 < len(lanes):  # transit north to the next lane
            gap = lanes[li + 1] - n
            for s in range(int(gap / speed * hz)):
                yield t, n + gap * s / int(gap / speed * hz), e1, 0.0
                t += dt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/demo")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--alt", type=float, default=30.0)
    args = ap.parse_args()
    rng = random.Random(args.seed)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cam = CameraModel.from_hfov(1920, 1080, 70.0)
    people = people_layout(rng)
    hz, speed = 10.0, 5.0
    dropout = (118.0, 126.0)  # seconds with no valid GPS fix

    with open(out / "ground_truth.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["truth_id", "lat", "lon"])
        for pid, n, e in people:
            lat, lon = offset_to_latlon(*ORIGIN, n, e)
            w.writerow([pid, f"{lat:.7f}", f"{lon:.7f}"])

    tel = open(out / "telemetry.csv", "w", newline="")
    tw = csv.writer(tel)
    tw.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop"])
    det = open(out / "detections.jsonl", "w")

    gps_bias = [rng.gauss(0, 1.0), rng.gauss(0, 1.0)]
    next_track = 1
    active: dict[str, int] = {}  # truth id -> current track id
    n_frames = 0
    for i, (t, n, e, hdg) in enumerate(flight_path(args.alt, speed, hz)):
        true_pitch, true_roll = rng.gauss(0, 1.5), rng.gauss(0, 1.5)
        alt = args.alt + rng.gauss(0, 0.3)
        lat, lon = offset_to_latlon(*ORIGIN, n, e)
        true_pose = Pose(lat, lon, alt, hdg, true_pitch, true_roll)

        # noisy telemetry (slow-wandering GPS bias + white noise)
        gps_bias = [b * 0.995 + rng.gauss(0, 0.05) for b in gps_bias]
        nlat, nlon = offset_to_latlon(lat, lon, gps_bias[0] + rng.gauss(0, 0.4), gps_bias[1] + rng.gauss(0, 0.4))
        fix = 0 if dropout[0] <= t <= dropout[1] else 3
        tw.writerow([f"{T0 + t:.3f}", f"{nlat:.8f}", f"{nlon:.8f}", f"{alt + rng.gauss(0, 0.8):.2f}",
                     f"{(hdg + rng.gauss(0, 1.5)) % 360:.2f}", f"{true_pitch + rng.gauss(0, 0.7):.2f}",
                     f"{true_roll + rng.gauss(0, 0.7):.2f}", fix, "0.9" if fix else "99"])

        dets = []
        visible = set()
        gsd = alt / cam.fx
        for pid, pn, pe in people:
            plat, plon = offset_to_latlon(*ORIGIN, pn, pe)
            px = ground_to_pixel(plat, plon, true_pose, cam)
            if px is None:
                continue
            visible.add(pid)
            if rng.random() < 0.08:  # missed detection this frame
                continue
            if pid not in active or rng.random() < 0.004:  # new track / ID switch
                active[pid] = next_track
                next_track += 1
            u, v = px
            bw, bh = 0.6 / gsd, 0.6 / gsd
            u += rng.gauss(0, 1.5)
            v += rng.gauss(0, 1.5)
            dets.append({"bbox": [round(u - bw / 2, 1), round(v - bh, 1), round(u + bw / 2, 1), round(v, 1)],
                         "conf": round(min(0.97, max(0.3, rng.gauss(0.75, 0.12))), 3),
                         "track_id": active[pid]})
        for pid in list(active):
            if pid not in visible:
                del active[pid]  # left the frame: next sighting gets a new track ID
        if rng.random() < 0.02:  # one-frame false positive
            u, v = rng.uniform(0, 1920), rng.uniform(0, 1080)
            dets.append({"bbox": [u - 10, v - 20, u + 10, v], "conf": 0.4, "track_id": next_track})
            next_track += 1
        det.write(json.dumps({"frame": i, "t": round(T0 + t, 3), "width": 1920, "height": 1080,
                              "detections": dets}) + "\n")
        n_frames += 1
    tel.close()
    det.close()
    print(f"wrote {out}: {len(people)} people, {n_frames} frames ({n_frames / hz:.0f} s), "
          f"{next_track - 1} track IDs")


if __name__ == "__main__":
    main()
