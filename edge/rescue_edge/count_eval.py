"""People-COUNT evaluation on aerial video with per-person IDs (VisDrone-MOT format).

Detection recall/precision (detector_eval.py) says whether boxes are right in single images.
It cannot say whether the mission's people count is right: one person can become several
tracks (split boxes, lost-and-restarted tracks) and still score fine per image. This module
scores tracks against ground-truth person IDs.

Ground truth (``annotations/<sequence>.txt``, one row per box):
``frame,target_id,x,y,w,h,score,category,truncation,occlusion``; frame numbers start at 1.
Person = category 1 (pedestrian) + 2 (people) with score 1. Category 0 = ignored region.

Per sequence, on the evaluated frames only:

* ``gt_people``: distinct person IDs visible in at least one evaluated frame.
* ``counted``: tracks seen in at least ``min_frames`` evaluated frames (the pipeline's rule).
* A counted track is **true** for the person ID it overlaps (IoU >= 0.5) in at least half of
  its frames; otherwise **false** (background, or mostly-unmatched fragments).
* ``found``: distinct person IDs with at least one true track.
* ``duplicates``: true tracks beyond the first per person (over-counting the same person).

Pure Python, no GPU, so it is unit-tested; scripts/eval_counting.py runs the detector.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass

from .detector_eval import Box, _inside, iou

PERSON_CATEGORIES = (1, 2)


@dataclass
class FrameTruth:
    people: dict[int, Box]          # target_id -> box
    ignore: list[Box]


def parse_mot(text: str) -> dict[int, FrameTruth]:
    frames: dict[int, FrameTruth] = defaultdict(lambda: FrameTruth({}, []))
    for line in text.splitlines():
        parts = [p for p in line.strip().split(",") if p != ""]
        if len(parts) < 8:
            continue
        f, tid, x, y, w, h = (int(float(v)) for v in parts[:6])
        score, cat = int(float(parts[6])), int(float(parts[7]))
        box = (float(x), float(y), float(x + w), float(y + h))
        if cat == 0:
            frames[f].ignore.append(box)
        elif cat in PERSON_CATEGORIES and score == 1:
            frames[f].people[tid] = box
    return dict(frames)


@dataclass
class CountResult:
    sequence: str = ""
    frames: int = 0
    gt_people: int = 0
    counted: int = 0
    found: int = 0
    false_tracks: int = 0
    duplicates: int = 0

    @property
    def count_error(self) -> int:
        return self.counted - self.gt_people

    def add(self, o: "CountResult") -> "CountResult":
        for k in ("frames", "gt_people", "counted", "found", "false_tracks", "duplicates"):
            setattr(self, k, getattr(self, k) + getattr(o, k))
        return self


def match_frame(tracks: list[tuple[int, Box]], truth: FrameTruth,
                iou_threshold: float = 0.5) -> dict[int, int | None]:
    """Track id -> GT person id (or None). Greedy by IoU, one-to-one within the frame.
    Tracks centred in an ignored region map to -1 (neither true nor false)."""
    pairs = sorted(((iou(b, g), t, p) for t, b in tracks for p, g in truth.people.items()),
                   reverse=True)
    out: dict[int, int | None] = {}
    used: set[int] = set()
    for v, t, p in pairs:
        if v < iou_threshold:
            break
        if t not in out and p not in used:
            out[t] = p
            used.add(p)
    for t, b in tracks:
        if t not in out:
            out[t] = -1 if any(_inside(b, r) for r in truth.ignore) else None
    return out


def score_sequence(tracks_by_frame: dict[int, list[tuple[int, Box]]],
                   truth: dict[int, FrameTruth], min_frames: int = 3,
                   name: str = "") -> CountResult:
    """``tracks_by_frame``: evaluated frame number -> [(track_id, box)]."""
    gt_ids: set[int] = set()
    seen: Counter[int] = Counter()
    hits: dict[int, Counter] = defaultdict(Counter)
    ignored: Counter[int] = Counter()
    for f, tracks in tracks_by_frame.items():
        ft = truth.get(f, FrameTruth({}, []))
        gt_ids.update(ft.people)
        for t, p in match_frame(tracks, ft).items():
            seen[t] += 1
            if p == -1:
                ignored[t] += 1
            elif p is not None:
                hits[t][p] += 1
    r = CountResult(sequence=name, frames=len(tracks_by_frame), gt_people=len(gt_ids))
    per_person: Counter[int] = Counter()
    for t, n in seen.items():
        if n < min_frames:
            continue
        if ignored[t] * 2 > n:          # mostly inside "ignore" areas: not scored
            continue
        r.counted += 1
        if hits[t]:
            p, k = hits[t].most_common(1)[0]
            if k * 2 >= n - ignored[t]:
                per_person[p] += 1
                continue
        r.false_tracks += 1
    r.found = len(per_person)
    r.duplicates = sum(c - 1 for c in per_person.values())
    return r
