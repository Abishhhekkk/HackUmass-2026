"""Person-detector evaluation: recall / precision at an IoU threshold (default 0.5).

Pure Python (no GPU, no Ultralytics) so it can be unit-tested; scripts/eval_detector.py runs
the model and feeds predictions in here.

Supported ground-truth formats (one label file per image, same stem):

* ``visdrone``: ``x,y,w,h,score,category,truncation,occlusion`` in pixels. Person = category
  1 (pedestrian) + 2 (people), merged. Category 0 = "ignored region": detections whose centre
  falls inside one are neither true nor false positives, as in the official VisDrone toolkit.
* ``yolo``: ``class cx cy w h`` normalised to 0..1 (SARD exports, your own labelled footage).
* ``voc``: Pascal VOC XML (HERIDAL). Person names default to human/person/pedestrian/people.

Recall is the priority metric for search and rescue: a missed person is worse than a false
alarm a human can dismiss.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 in pixels

SMALL_AREA_PX = 32 * 32  # COCO "small"; most aerial people are below this
VOC_PERSON_NAMES = ("human", "person", "pedestrian", "people")
VISDRONE_PERSON = (1, 2)


@dataclass
class ImageTruth:
    people: list[Box] = field(default_factory=list)
    ignore: list[Box] = field(default_factory=list)


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    gt_small: int = 0
    tp_small: int = 0
    images: int = 0

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def f1(self) -> float | None:
        r, p = self.recall, self.precision
        return 2 * p * r / (p + r) if p and r else None

    @property
    def recall_small(self) -> float | None:
        return self.tp_small / self.gt_small if self.gt_small else None


# ------------------------------------------------------------------ parsing
def parse_visdrone(text: str, person_categories=VISDRONE_PERSON) -> ImageTruth:
    truth = ImageTruth()
    for line in text.splitlines():
        parts = [p for p in line.replace(" ", "").split(",") if p != ""]
        if len(parts) < 6:
            continue
        x, y, w, h = (float(v) for v in parts[:4])
        score, category = int(float(parts[4])), int(float(parts[5]))
        box = (x, y, x + w, y + h)
        if category == 0:
            truth.ignore.append(box)
        elif category in person_categories and score != 0:
            truth.people.append(box)
    return truth


def parse_yolo(text: str, width: int, height: int, person_classes=(0,)) -> ImageTruth:
    truth = ImageTruth()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or int(float(parts[0])) not in person_classes:
            continue
        cx, cy, w, h = (float(v) for v in parts[1:5])
        truth.people.append(((cx - w / 2) * width, (cy - h / 2) * height,
                             (cx + w / 2) * width, (cy + h / 2) * height))
    return truth


def parse_voc(text: str, person_names=VOC_PERSON_NAMES) -> ImageTruth:
    truth = ImageTruth()
    names = {n.lower() for n in person_names}
    for obj in ET.fromstring(text).iter("object"):
        if (obj.findtext("name") or "").strip().lower() not in names:
            continue
        bb = obj.find("bndbox")
        if bb is None:
            continue
        truth.people.append(tuple(float(bb.findtext(k)) for k in ("xmin", "ymin", "xmax", "ymax")))
    return truth


def label_path(labels_dir: Path, image: Path, fmt: str) -> Path:
    return labels_dir / (image.stem + (".xml" if fmt == "voc" else ".txt"))


def load_truth(labels_dir: Path, image: Path, fmt: str, width: int, height: int,
               person_classes=None) -> ImageTruth:
    path = label_path(labels_dir, image, fmt)
    if not path.is_file():
        return ImageTruth()  # image with no people
    text = path.read_text(encoding="utf-8", errors="replace")
    if fmt == "visdrone":
        return parse_visdrone(text, person_classes or VISDRONE_PERSON)
    if fmt == "yolo":
        return parse_yolo(text, width, height, person_classes or (0,))
    if fmt == "voc":
        return parse_voc(text, person_classes or VOC_PERSON_NAMES)
    raise ValueError(f"unknown label format {fmt!r}")


# ------------------------------------------------------------------ matching
def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _inside(box: Box, region: Box) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def match_image(preds: list[tuple[Box, float]], truth: ImageTruth, conf: float,
                iou_threshold: float = 0.5) -> Counts:
    """Greedy matching by descending confidence; each ground-truth box matches at most once."""
    kept = sorted((p for p in preds if p[1] >= conf), key=lambda p: -p[1])
    matched = [False] * len(truth.people)
    c = Counts(images=1)
    for box, _score in kept:
        best, best_iou = -1, iou_threshold
        for i, gt in enumerate(truth.people):
            if not matched[i]:
                v = iou(box, gt)
                if v >= best_iou:
                    best, best_iou = i, v
        if best >= 0:
            matched[best] = True
            c.tp += 1
        elif not any(_inside(box, r) for r in truth.ignore):
            c.fp += 1
    for i, gt in enumerate(truth.people):
        small = (gt[2] - gt[0]) * (gt[3] - gt[1]) < SMALL_AREA_PX
        c.gt_small += small
        c.tp_small += small and matched[i]
    c.fn = matched.count(False)
    return c


def add(total: Counts, c: Counts) -> Counts:
    for k in ("tp", "fp", "fn", "gt_small", "tp_small", "images"):
        setattr(total, k, getattr(total, k) + getattr(c, k))
    return total
