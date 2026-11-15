"""Training-data helpers: cut labelled aerial images into tiles for a single-class person model.

Why tiles: the Jetson runs a fixed 640 px engine on 640 px (or smaller) crops of each frame
(see tiling.py). Training on 640 px crops at native resolution shows the model people at the
same size it will see them in flight, instead of shrinking whole frames until people vanish.

Pure numpy; the script that reads images and writes files is scripts/prepare_visdrone.py.
"""
from __future__ import annotations

Box = tuple[float, float, float, float]  # x1, y1, x2, y2, frame pixels


def boxes_in_tile(people: list[Box], window: tuple[int, int, int, int],
                  min_visible: float = 0.5) -> tuple[list[Box], list[Box]]:
    """Split people into (kept, partial) for one tile, in tile coordinates.

    kept: at least ``min_visible`` of the box area inside the tile (clipped to the tile).
    partial: some of it inside but less than ``min_visible``. These are neither labelled nor
    left as unlabelled people (which would teach the model to ignore people): the caller
    blanks them out of the image, like VisDrone's ignored regions.
    """
    x0, y0, x1, y1 = window
    kept, partial = [], []
    for bx1, by1, bx2, by2 in people:
        area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        cx1, cy1, cx2, cy2 = max(bx1, x0), max(by1, y0), min(bx2, x1), min(by2, y1)
        inside = max(0.0, cx2 - cx1) * max(0.0, cy2 - cy1)
        if area <= 0 or inside <= 0:
            continue
        clipped = (cx1 - x0, cy1 - y0, cx2 - x0, cy2 - y0)
        (kept if inside / area >= min_visible else partial).append(clipped)
    return kept, partial


def regions_in_tile(regions: list[Box], window: tuple[int, int, int, int]) -> list[Box]:
    """Ignored regions clipped to the tile, in tile coordinates."""
    x0, y0, x1, y1 = window
    out = []
    for rx1, ry1, rx2, ry2 in regions:
        cx1, cy1, cx2, cy2 = max(rx1, x0), max(ry1, y0), min(rx2, x1), min(ry2, y1)
        if cx2 > cx1 and cy2 > cy1:
            out.append((cx1 - x0, cy1 - y0, cx2 - x0, cy2 - y0))
    return out


def yolo_label_lines(boxes: list[Box], width: int, height: int, cls: int = 0) -> list[str]:
    """YOLO txt lines: ``class cx cy w h`` normalised to 0..1."""
    lines = []
    for x1, y1, x2, y2 in boxes:
        w, h = x2 - x1, y2 - y1
        if w < 1 or h < 1:
            continue
        lines.append(f"{cls} {(x1 + w / 2) / width:.6f} {(y1 + h / 2) / height:.6f} "
                     f"{w / width:.6f} {h / height:.6f}")
    return lines
