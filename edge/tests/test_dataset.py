import pytest

from rescue_edge.dataset import boxes_in_tile, regions_in_tile, yolo_label_lines


def test_boxes_in_tile_keeps_mostly_visible_people_and_flags_cut_ones():
    window = (100, 100, 740, 740)
    people = [
        (200, 200, 210, 225),   # fully inside
        (95, 300, 105, 325),    # half inside the left edge -> kept (>= 50%), clipped
        (735, 300, 745, 325),   # half inside the right edge -> kept, clipped
        (737, 400, 747, 425),   # 30% inside -> partial (to be blanked)
        (900, 900, 910, 925),   # outside
    ]
    kept, partial = boxes_in_tile(people, window)
    assert kept == [(100, 100, 110, 125), (0, 200, 5, 225), (635, 200, 640, 225)]
    assert partial == [(637, 300, 640, 325)]


def test_regions_clipped_to_tile():
    assert regions_in_tile([(0, 0, 150, 150), (800, 800, 900, 900)], (100, 100, 740, 740)) == [
        (0, 0, 50, 50)]


def test_yolo_label_lines_normalised_and_skips_slivers():
    lines = yolo_label_lines([(0, 0, 64, 128), (10, 10, 10.5, 30)], 640, 640)
    assert lines == ["0 0.050000 0.100000 0.100000 0.200000"]
    cls, cx, cy, w, h = lines[0].split()
    assert cls == "0" and float(w) == pytest.approx(0.1)
