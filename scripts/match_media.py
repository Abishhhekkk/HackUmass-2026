#!/usr/bin/env python3
"""Match saved photos and video to estimated person locations (all offline, on the Jetson or after
copying the mission folder off it).

    python scripts/match_media.py data/edge/snapshots/<mission_id>

Reads, from the mission folder:
    photos/photos_index.csv        one row per first-detection photo (time, track key, drone pose)
    video/seg_*.frames.csv         true GPS-clock time of every recorded video frame
    v<NNNNNN>/sightings.csv        the newest snapshot: estimated person positions

and writes <mission_folder>/media_matches.csv with one row per photo:
    photo, track_key, frame_t_utc, status, person_id, lat, lon, horizontal_error_m,
    unlocated_reason, drone_lat, drone_lon, video_file, video_frame_no, video_s_from_segment_start

How rows are matched:
  * photo -> person: the photo's track_key (pass:track_id) appears in sightings.csv `track_ids`.
    status is "estimated_sighting" (has lat/lon), "unlocated_sighting" (seen, but no usable
    position; the reason is given) or "not_confirmed" (the track never lasted long enough to count
    as a person, or no snapshot exists yet).
  * photo -> video: the frame whose recorded time is closest to the photo's frame time.
    To pull that exact frame out of a segment:
        ffmpeg -i seg_X.mkv -vf "select=eq(n\\,FRAME_NO)" -vframes 1 frame.jpg
"""
from __future__ import annotations

import argparse
import bisect
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "edge"))
from rescue_edge.snapshot import latest_version  # noqa: E402

OUT_FIELDS = ["photo", "track_key", "frame_t_utc", "status", "person_id", "lat", "lon",
              "horizontal_error_m", "unlocated_reason", "drone_lat", "drone_lon",
              "video_file", "video_frame_no", "video_s_from_segment_start"]
VIDEO_TOLERANCE_S = 1.0     # a photo this close to a segment's ends still maps to its edge frame


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _segments(video_dir: Path) -> list[dict]:
    """Segments come from the per-frame logs, so they survive a power cut before the index row."""
    out = []
    for frames_csv in sorted(video_dir.glob("seg_*.frames.csv")):
        rows = _read(frames_csv)
        if not rows:
            continue
        stem = frames_csv.name[: -len(".frames.csv")]
        video = next((video_dir / (stem + ext) for ext in (".mkv", ".avi", ".mp4")
                      if (video_dir / (stem + ext)).exists()), None)
        out.append({"file": video.name if video else stem + " (video file missing)",
                    "times": [float(r["frame_t_epoch"]) for r in rows],
                    "frame_nos": [int(r["frame_no"]) for r in rows]})
    return out


def _find_frame(segments: list[dict], t: float) -> dict:
    best = None
    for s in segments:
        lo, hi = s["times"][0], s["times"][-1]
        if lo - VIDEO_TOLERANCE_S <= t <= hi + VIDEO_TOLERANCE_S:
            gap = 0.0 if lo <= t <= hi else min(abs(t - lo), abs(t - hi))
            if best is None or gap < best[0]:
                best = (gap, s)
    if best is None:
        return {"video_file": "", "video_frame_no": "", "video_s_from_segment_start": ""}
    s = best[1]
    i = bisect.bisect_left(s["times"], t)
    cands = [j for j in (i - 1, i) if 0 <= j < len(s["times"])]
    j = min(cands, key=lambda k: abs(s["times"][k] - t))
    return {"video_file": s["file"], "video_frame_no": s["frame_nos"][j],
            "video_s_from_segment_start": f"{s['times'][j] - s['times'][0]:.3f}"}


def match(mission_dir: str | Path) -> list[dict]:
    mission_dir = Path(mission_dir)
    version = latest_version(mission_dir)
    sightings = _read(mission_dir / f"v{version:06d}" / "sightings.csv") if version else []

    by_track: dict[str, dict] = {}
    for row in sightings:
        located = row["status"] == "estimated_sighting"
        for key in filter(None, row["track_ids"].split(";")):
            # a track can appear on both a located and an unlocated row: prefer the located one
            if key not in by_track or (located and by_track[key]["status"] != "estimated_sighting"):
                by_track[key] = row

    segments = _segments(mission_dir / "video")
    out = []
    for p in _read(mission_dir / "photos" / "photos_index.csv"):
        s = by_track.get(p["track_key"])
        out.append({
            "photo": p["file"], "track_key": p["track_key"], "frame_t_utc": p["frame_t_utc"],
            "status": s["status"] if s else "not_confirmed",
            "person_id": s["person_id"] if s else "",
            "lat": s["lat"] if s else "", "lon": s["lon"] if s else "",
            "horizontal_error_m": s["horizontal_error_m"] if s else "",
            "unlocated_reason": s["unlocated_reason"] if s else "",
            "drone_lat": p["drone_lat"], "drone_lon": p["drone_lon"],
            **_find_frame(segments, float(p["frame_t_epoch"])),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mission_dir", type=Path, help="e.g. data/edge/snapshots/<mission_id>")
    ap.add_argument("--out", type=Path, help="default: <mission_dir>/media_matches.csv")
    args = ap.parse_args()
    if not args.mission_dir.is_dir():
        raise SystemExit(f"not a folder: {args.mission_dir}")
    rows = match(args.mission_dir)
    out = args.out or args.mission_dir / "media_matches.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    located = sum(r["status"] == "estimated_sighting" for r in rows)
    print(f"{len(rows)} photos, {located} matched to a located person -> {out}")
    for r in rows:
        where = f"{r['lat']}, {r['lon']}" if r["lat"] else (r["unlocated_reason"] or "no location")
        print(f"  {r['photo']:<34} {r['status']:<20} {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
