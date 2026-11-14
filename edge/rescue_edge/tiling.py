"""Tiled inference for small, distant people (pure numpy; no Ultralytics needed here).

A 1920 px frame squeezed into a 640 px model input shrinks a 25 px person to ~8 px, which
a stride-8 detector barely sees. Tiling runs the same fixed-size model (e.g. the 640 TensorRT
engine) on overlapping crops, so each person keeps (or gains) pixels:

* ``tile_grid`` lays out overlapping windows that cover the whole frame;
* detections from each tile are shifted back to frame coordinates;
* ``merge_detections`` removes the duplicates that overlaps and tile borders create.

Cost is roughly one model run per tile, so choose the tile size from measured FPS.
"""
from __future__ import annotations

import math

import numpy as np

Window = tuple[int, int, int, int]  # x0, y0, x1, y1 in frame pixels


def tile_grid(width: int, height: int, tile: int, overlap: float = 0.2) -> list[Window]:
    """Overlapping tile windows covering the frame; the last row/column is aligned to the
    far edge, so tiles never extend past the image and are never padded."""
    if tile <= 0 or not 0 <= overlap < 1:
        raise ValueError("tile must be positive and overlap in [0, 1)")

    def starts(length: int) -> list[int]:
        if length <= tile:
            return [0]
        step = max(1, int(tile * (1 - overlap)))
        n = math.ceil((length - tile) / step) + 1
        return sorted({min(i * step, length - tile) for i in range(n)})

    tw, th = min(tile, width), min(tile, height)
    return [(x, y, x + tw, y + th) for y in starts(height) for x in starts(width)]


def _iou_ios(box: np.ndarray, others: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    ix = np.clip(np.minimum(box[2], others[:, 2]) - np.maximum(box[0], others[:, 0]), 0, None)
    iy = np.clip(np.minimum(box[3], others[:, 3]) - np.maximum(box[1], others[:, 1]), 0, None)
    inter = ix * iy
    area = (box[2] - box[0]) * (box[3] - box[1])
    areas = (others[:, 2] - others[:, 0]) * (others[:, 3] - others[:, 1])
    union = area + areas - inter
    smaller = np.minimum(area, areas)
    return (np.where(union > 0, inter / np.maximum(union, 1e-9), 0.0),
            np.where(smaller > 0, inter / np.maximum(smaller, 1e-9), 0.0))


def merge_detections(boxes, scores, iou_threshold: float = 0.5,
                     ios_threshold: float = 0.8) -> tuple[np.ndarray, np.ndarray]:
    """Greedy NMS over all tiles. Besides IoU, a box mostly inside a higher-scoring one
    (intersection over the smaller box >= ios_threshold) is dropped: that is the partial
    box a tile border cuts off a person who also appears whole in the neighbouring tile."""
    boxes = np.asarray(boxes, dtype=float).reshape(-1, 4)
    scores = np.asarray(scores, dtype=float).reshape(-1)
    order = np.argsort(-scores, kind="stable")
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        if order.size == 1:
            break
        iou, ios = _iou_ios(boxes[i], boxes[order[1:]])
        order = order[1:][(iou < iou_threshold) & (ios < ios_threshold)]
    keep = np.array(keep, dtype=int)
    return boxes[keep], scores[keep]


def predict_tiled(predict, image: np.ndarray, tile: int, overlap: float = 0.2,
                  full_frame: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """Run ``predict(crop) -> (boxes_xyxy, scores)`` on every tile (and optionally the whole
    frame, which catches people too large for one tile), shift tile boxes back into frame
    coordinates and merge duplicates."""
    h, w = image.shape[:2]
    all_boxes, all_scores = [], []
    for x0, y0, x1, y1 in tile_grid(w, h, tile, overlap):
        b, s = predict(image[y0:y1, x0:x1])
        b = np.asarray(b, dtype=float).reshape(-1, 4)
        if len(b):
            all_boxes.append(b + [x0, y0, x0, y0])
            all_scores.append(np.asarray(s, dtype=float).reshape(-1))
    if full_frame:
        b, s = predict(image)
        b = np.asarray(b, dtype=float).reshape(-1, 4)
        if len(b):
            all_boxes.append(b)
            all_scores.append(np.asarray(s, dtype=float).reshape(-1))
    if not all_boxes:
        return np.zeros((0, 4)), np.zeros(0)
    return merge_detections(np.concatenate(all_boxes), np.concatenate(all_scores))
