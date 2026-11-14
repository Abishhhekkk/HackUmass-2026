import numpy as np
import pytest

from rescue_edge.tiling import merge_detections, predict_tiled, tile_grid


def test_tile_grid_covers_frame_without_padding():
    tiles = tile_grid(1920, 1080, 640, overlap=0.2)
    assert all(0 <= x0 < x1 <= 1920 and 0 <= y0 < y1 <= 1080 for x0, y0, x1, y1 in tiles)
    assert all(x1 - x0 == 640 and y1 - y0 == 640 for x0, y0, x1, y1 in tiles)
    covered = np.zeros((1080, 1920), bool)
    for x0, y0, x1, y1 in tiles:
        covered[y0:y1, x0:x1] = True
    assert covered.all()
    assert len(tiles) == 4 * 2  # 1920: 0,512,1024,1280 ; 1080: 0,440
    assert tile_grid(500, 300, 640) == [(0, 0, 500, 300)]  # smaller than a tile: one window


def test_merge_drops_overlap_duplicates_and_border_fragments():
    boxes = [
        [100, 100, 110, 125],   # person seen in tile A
        [101, 100, 111, 125],   # same person seen in overlapping tile B (lower score)
        [100, 100, 110, 112],   # border-cut fragment of the same person (inside the full box)
        [300, 300, 310, 325],   # a different person
    ]
    kept, scores = merge_detections(boxes, [0.9, 0.8, 0.85, 0.7])
    assert kept.tolist() == [[100, 100, 110, 125], [300, 300, 310, 325]]
    assert scores.tolist() == [0.9, 0.7]


def test_merge_keeps_adjacent_people_in_a_crowd():
    boxes = [[100, 100, 110, 125], [111, 100, 121, 125], [122, 101, 132, 126]]
    kept, _ = merge_detections(boxes, [0.9, 0.8, 0.7])
    assert len(kept) == 3


def test_predict_tiled_maps_boxes_back_to_frame_coordinates():
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[700:725, 1500:1510] = 255  # one bright "person"

    def fake_predict(crop):
        ys, xs = np.nonzero(crop[..., 0])
        if not len(xs):
            return np.zeros((0, 4)), np.zeros(0)
        return np.array([[xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]]), np.array([0.9])

    boxes, scores = predict_tiled(fake_predict, img, tile=640, overlap=0.2, full_frame=True)
    assert boxes.tolist() == [[1500, 700, 1510, 725]]  # seen by several tiles + full frame: kept once
    with pytest.raises(ValueError):
        tile_grid(100, 100, 0)
