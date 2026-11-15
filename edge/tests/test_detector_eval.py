import pytest

from rescue_edge.detector_eval import (Counts, ImageTruth, add, iou, load_truth, match_image,
                                       parse_visdrone, parse_voc, parse_yolo)


def test_iou_basics():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(50 / 150)


def test_visdrone_merges_pedestrian_and_people_and_keeps_ignore_regions():
    text = "\n".join([
        "10,10,8,20,1,1,0,0",     # pedestrian
        "40,10,8,20,1,2,0,0",     # people (merged into person)
        "70,10,8,20,1,4,0,0",     # car: not a person
        "100,10,8,20,0,1,0,0",    # score 0: ignored annotation
        "200,200,50,50,0,0,0,0",  # category 0: ignored region
    ])
    truth = parse_visdrone(text)
    assert truth.people == [(10, 10, 18, 30), (40, 10, 48, 30)]
    assert truth.ignore == [(200, 200, 250, 250)]


def test_yolo_and_voc_parsers():
    truth = parse_yolo("0 0.5 0.5 0.1 0.2\n2 0.1 0.1 0.1 0.1\n", 1000, 500)
    assert truth.people == [pytest.approx((450, 200, 550, 300))]
    xml = """<annotation><object><name>Human</name><bndbox><xmin>1</xmin><ymin>2</ymin>
      <xmax>11</xmax><ymax>22</ymax></bndbox></object><object><name>car</name><bndbox>
      <xmin>0</xmin><ymin>0</ymin><xmax>5</xmax><ymax>5</ymax></bndbox></object></annotation>"""
    assert parse_voc(xml).people == [(1, 2, 11, 22)]


def test_matching_counts_tp_fp_fn_and_respects_conf_and_ignore():
    truth = ImageTruth(people=[(0, 0, 10, 20), (100, 0, 110, 20)], ignore=[(200, 0, 300, 100)])
    preds = [
        ((0, 0, 10, 20), 0.9),       # TP
        ((1, 0, 11, 20), 0.8),       # duplicate of the same person -> FP
        ((250, 40, 260, 60), 0.7),   # inside ignore region -> neither
        ((500, 0, 510, 20), 0.2),    # false alarm below conf 0.25 -> dropped
    ]
    c = match_image(preds, truth, conf=0.25)
    assert (c.tp, c.fp, c.fn) == (1, 1, 1)
    assert c.recall == 0.5 and c.precision == 0.5
    low = match_image(preds, truth, conf=0.1)
    assert (low.tp, low.fp, low.fn) == (1, 2, 1)


def test_small_people_recall_and_totals():
    truth = ImageTruth(people=[(0, 0, 10, 20), (100, 0, 160, 80)])  # 200 px^2 small, 4800 large
    c = match_image([((100, 0, 160, 80), 0.9)], truth, conf=0.1)
    assert (c.gt_small, c.tp_small) == (1, 0) and c.recall_small == 0.0
    total = add(add(Counts(), c), c)
    assert (total.tp, total.fn, total.images) == (2, 2, 2) and total.recall == 0.5


def test_missing_label_file_means_no_people(tmp_path):
    img = tmp_path / "frame1.jpg"
    assert load_truth(tmp_path, img, "visdrone", 100, 100).people == []
    (tmp_path / "frame1.txt").write_text("1,1,5,5,1,1,0,0\n")
    assert len(load_truth(tmp_path, img, "visdrone", 100, 100).people) == 1
