#!/usr/bin/env python3
"""Compare a snapshot's estimated people against known ground truth.

    python scripts/evaluate.py data/edge/snapshots/demo-mission data/demo/ground_truth.csv

Use the same script on the real test field: survey/mark where your volunteers stand,
put them in a ground_truth.csv, fly, and report these numbers in the demo.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
from rescue_edge.geolocate import latlon_to_offset  # noqa: E402
from rescue_edge.snapshot import latest_version, snapshot_dir  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mission_dir")
    ap.add_argument("ground_truth")
    ap.add_argument("--match-radius-m", type=float, default=10.0)
    a = ap.parse_args()
    md = Path(a.mission_dir)
    v = latest_version(md)
    rows = list(csv.DictReader(open(snapshot_dir(md.parent, md.name, v) / "sightings.csv")))
    est = [(r["person_id"], float(r["lat"]), float(r["lon"]), float(r["horizontal_error_m"]))
           for r in rows if r["status"] == "estimated_sighting"]
    unloc = [r for r in rows if r["status"] != "estimated_sighting"]
    truth = [(r["truth_id"], float(r["lat"]), float(r["lon"])) for r in csv.DictReader(open(a.ground_truth))]

    pairs = []
    for e in est:
        for t in truth:
            n, ea = latlon_to_offset(t[1], t[2], e[1], e[2])
            d = math.hypot(n, ea)
            if d <= a.match_radius_m:
                pairs.append((d, e[0], t[0], e[3]))
    pairs.sort()
    used_e, used_t, errs, inside = set(), set(), [], 0
    for d, eid, tid, claimed in pairs:
        if eid in used_e or tid in used_t:
            continue
        used_e.add(eid)
        used_t.add(tid)
        errs.append(d)
        inside += d <= 2 * claimed  # within the ~2-sigma circle we draw
    errs.sort()
    p90 = errs[min(len(errs) - 1, int(round(0.9 * (len(errs) - 1))))] if errs else None
    print(f"snapshot v{v}")
    print(f"ground-truth people : {len(truth)}")
    print(f"estimated people    : {len(est)}  (+{len(unloc)} unlocated sightings)")
    print(f"matched (<= {a.match_radius_m:g} m)  : {len(errs)}")
    print(f"missed              : {len(truth) - len(used_t)}")
    print(f"extra (dup/false)   : {len(est) - len(used_e)}")
    if errs:
        print(f"position error      : median {statistics.median(errs):.2f} m, p90 {p90:.2f} m, "
              f"max {errs[-1]:.2f} m")
        print(f"inside 2x claimed error circle: {inside}/{len(errs)}")


if __name__ == "__main__":
    main()
