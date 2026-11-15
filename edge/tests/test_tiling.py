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


def test_person_larger_than_a_tile_is_not_split_into_fragments():
    """A big (near) person spans several tiles; each tile sees a fragment. Fragments touching
    an inner tile edge are dropped and the full-frame pass reports the person once."""
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[200:700, 900:1150] = 255  # 250 x 500 px person, bigger than a 320 px tile

    def fake_predict(crop):  # "detects" whatever bright region is in view, high score in tiles
        ys, xs = np.nonzero(crop[..., 0])
        if not len(xs):
            return np.zeros((0, 4)), np.zeros(0)
        score = 0.5 if crop.shape[:2] == (1080, 1920) else 0.9  # fragments score higher
        return np.array([[xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]]), np.array([score])

    boxes, _ = predict_tiled(fake_predict, img, tile=320, overlap=0.2, full_frame=True)
    assert boxes.tolist() == [[900, 200, 1150, 700]]


def test_box_on_the_image_border_is_kept():
    img = np.zeros((1080, 1920, 3), np.uint8)
    img[0:20, 0:10] = 255  # person at the very top-left corner of the frame

    def fake_predict(crop):
        ys, xs = np.nonzero(crop[..., 0])
        if not len(xs):
            return np.zeros((0, 4)), np.zeros(0)
        return np.array([[xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]]), np.array([0.9])

    boxes, _ = predict_tiled(fake_predict, img, tile=320, overlap=0.2, full_frame=False)
    assert boxes.tolist() == [[0, 0, 10, 20]]


def test_tiled_tracker_merges_tiles_then_tracks():
    """No Ultralytics needed: fake model + fake ByteTrack check the wiring."""
    from types import SimpleNamespace

    from rescue_edge.capture import Frame
    from rescue_edge.detector import TiledYoloTracker, build_detector

    img = np.zeros((1080, 1920, 3), np.uint8)
    img[700:725, 1500:1510] = 255  # one small "person"
    calls = []

    class FakeTensor:
        def __init__(self, a):
            self.a = np.asarray(a, dtype=float)

        def cpu(self):
            return self

        def numpy(self):
            return self.a

    class FakeModel:
        def predict(self, crop, **kw):
            calls.append((crop.shape[:2], kw["imgsz"], kw["conf"], tuple(kw["classes"])))
            ys, xs = np.nonzero(crop[..., 0])
            b = [[xs.min(), ys.min(), xs.max() + 1, ys.max() + 1]] if len(xs) else np.zeros((0, 4))
            s = [0.6] if len(xs) else []
            return [SimpleNamespace(boxes=SimpleNamespace(xyxy=FakeTensor(b), conf=FakeTensor(s)))]

    class FakeByteTrack:
        def __init__(self):
            self.seen = []

        def update(self, dets, img):
            dets = np.asarray(dets)
            self.seen.append(dets)
            return np.array([[*d[:4], 7, d[4], 0, i] for i, d in enumerate(dets)])

    tracker = FakeByteTrack()
    det = TiledYoloTracker(imgsz=640, conf_threshold=0.1, tile=320, model=FakeModel(),
                           tracker_impl=tracker)
    out = det(Frame(0, 0.0, img, 1920, 1080))
    assert len(calls) == 8 * 4 + 1                      # 32 tiles of 320 px + 1 full frame
    assert {c[1:] for c in calls} == {(640, 0.1, (0,))}  # fixed engine size, person only
    assert tracker.seen[0].shape == (1, 6)               # merged to one box before tracking
    assert out[0].bbox_xyxy == (1500, 700, 1510, 725) and out[0].track_id == 7
    assert out[0].confidence == pytest.approx(0.6)


def test_build_detector_uses_tiling_only_when_configured(monkeypatch):
    import rescue_edge.detector as detmod

    made = []
    monkeypatch.setattr(detmod, "TiledYoloTracker", lambda **kw: made.append(("tiled", kw)))
    monkeypatch.setattr(detmod, "YoloTracker", lambda **kw: made.append(("whole", kw)))
    detmod.build_detector({"kind": "yolo", "tile": 320, "tile_overlap": 0.25})
    detmod.build_detector({"kind": "yolo"})
    assert [m[0] for m in made] == ["tiled", "whole"]
    assert made[0][1]["tile"] == 320 and made[0][1]["tile_overlap"] == 0.25
