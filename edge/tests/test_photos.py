"""First-sighting photo writer. Free of fastapi so it runs anywhere with OpenCV."""
import csv
import threading
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from rescue_edge.capture import Frame  # noqa: E402
from rescue_edge.detector import Detection  # noqa: E402
from rescue_edge.geolocate import Pose  # noqa: E402
from rescue_edge.main import Pipeline  # noqa: E402
from rescue_edge.photos import INDEX_FIELDS, PhotoWriter  # noqa: E402
from rescue_edge.report import iso  # noqa: E402

T0 = 1794668400.120
POSE = Pose(42.3890100, -72.5283000, 24.0, 91.0, 0.8, -1.2)


def scene(w=1920, h=1080):
    """A smooth gradient with a bright box: compresses like real footage, unlike pure noise."""
    x = np.linspace(0, 255, w, dtype=np.uint8)[None, :].repeat(h, 0)
    y = np.linspace(0, 255, h, dtype=np.uint8)[:, None].repeat(w, 1)
    img = np.stack([x, y, (x // 2 + y // 2).astype(np.uint8)], axis=2)
    img[400:700, 900:1000] = 255
    return img


def rows(writer):
    with open(writer.index_path, newline="") as f:
        return list(csv.DictReader(f))


def run_and_flush(w):
    w.start()
    w.stop()


def test_saves_decodable_compressed_jpeg_and_index_row(tmp_path):
    w = PhotoWriter(tmp_path, "m1", quality=90)
    img = scene()
    assert w.submit(img, T0, 17, "p1:42", 0.83, (900.0, 400.0, 1000.0, 700.0), POSE)
    run_and_flush(w)

    (r,) = rows(w)
    assert r["file"] == "p1_42_1794668400120.jpg" and r["track_key"] == "p1:42"
    jpg = w.dir / r["file"]
    assert jpg.exists() and not list(w.dir.glob("*.tmp"))
    assert int(r["jpeg_bytes"]) == jpg.stat().st_size
    assert jpg.stat().st_size < img.nbytes / 10          # real compression, not just a re-save
    decoded = cv2.imdecode(np.frombuffer(jpg.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == img.shape
    assert r["image_width"] == "1920" and r["image_height"] == "1080"
    assert r["frame_t_utc"] == iso(T0) and r["frame_t_utc"].endswith(".120Z")
    assert float(r["frame_t_epoch"]) == pytest.approx(T0)
    assert r["frame_index"] == "17" and float(r["confidence"]) == 0.83
    assert (r["bbox_x1"], r["bbox_y2"]) == ("900.0", "700.0")
    assert r["drone_lat"] == "42.3890100" and r["drone_lon"] == "-72.5283000"
    assert r["drone_heading_deg"] == "91.0" and r["drone_pose_unavailable_reason"] == ""
    assert set(r) == set(INDEX_FIELDS)


def test_lower_quality_gives_smaller_files(tmp_path):
    sizes = {}
    for q in (95, 60):
        w = PhotoWriter(tmp_path / str(q), "m1", quality=q)
        w.submit(scene(), T0, 0, "p1:1", 0.9, (0, 0, 1, 1), POSE)
        run_and_flush(w)
        sizes[q] = int(rows(w)[0]["jpeg_bytes"])
    assert sizes[60] < sizes[95]


def test_missing_pose_is_recorded_with_reason_not_invented(tmp_path):
    w = PhotoWriter(tmp_path, "m1")
    w.submit(scene(320, 240), T0, 3, "p1:7", 0.5, (1, 2, 3, 4), None, "gnss_fix_invalid")
    run_and_flush(w)
    (r,) = rows(w)
    assert r["drone_lat"] == "" and r["drone_lon"] == ""
    assert r["drone_pose_unavailable_reason"] == "gnss_fix_invalid"


def test_one_photo_per_track(tmp_path):
    w = PhotoWriter(tmp_path, "m1")
    img = scene(320, 240)
    assert w.submit(img, T0, 1, "p1:1", 0.9, (0, 0, 1, 1), POSE) is True
    assert w.submit(img, T0 + 0.1, 2, "p1:1", 0.9, (0, 0, 1, 1), POSE) is False  # same track again
    assert w.submit(img, T0 + 0.1, 2, "p1:2", 0.9, (0, 0, 1, 1), POSE) is True
    assert w.submit(img, T0 + 5, 9, "p2:1", 0.9, (0, 0, 1, 1), POSE) is True      # new pass, same id
    run_and_flush(w)
    assert sorted(r["track_key"] for r in rows(w)) == ["p1:1", "p1:2", "p2:1"]
    assert w.status()["saved"] == 3


def test_restart_does_not_rephotograph_known_tracks(tmp_path):
    w = PhotoWriter(tmp_path, "m1")
    w.submit(scene(320, 240), T0, 1, "p1:1", 0.9, (0, 0, 1, 1), POSE)
    run_and_flush(w)
    again = PhotoWriter(tmp_path, "m1")
    assert again.submit(scene(320, 240), T0 + 9, 99, "p1:1", 0.9, (0, 0, 1, 1), POSE) is False
    assert again.submit(scene(320, 240), T0 + 9, 99, "p1:2", 0.9, (0, 0, 1, 1), POSE) is True


def test_full_queue_never_blocks_and_track_is_retried_later(tmp_path):
    w = PhotoWriter(tmp_path, "m1", queue_size=1)      # writer thread not started: queue stays full
    img = scene(320, 240)
    done = threading.Event()

    def submit_both():
        assert w.submit(img, T0, 1, "p1:1", 0.9, (0, 0, 1, 1), POSE) is True
        assert w.submit(img, T0, 2, "p1:2", 0.9, (0, 0, 1, 1), POSE) is False  # would block => skipped
        done.set()

    threading.Thread(target=submit_both, daemon=True).start()
    assert done.wait(2.0), "submit() blocked on a full queue"
    assert w.dropped == 1 and "p1:2" not in w.known     # not marked done, so it is offered again
    w.start()
    w.stop()
    assert w.submit(img, T0 + 1, 3, "p1:2", 0.9, (0, 0, 1, 1), POSE) is True  # retry accepted


def test_photo_cap(tmp_path):
    w = PhotoWriter(tmp_path, "m1", max_photos=2)
    img = scene(320, 240)
    assert [w.submit(img, T0 + i, i, f"p1:{i}", 0.9, (0, 0, 1, 1), POSE) for i in range(4)] == \
        [True, True, False, False]
    run_and_flush(w)
    assert len(rows(w)) == 2


def test_a_bad_image_does_not_kill_the_writer(tmp_path):
    w = PhotoWriter(tmp_path, "m1")
    w.submit(np.zeros((0, 0, 3), np.uint8), T0, 1, "p1:1", 0.9, (0, 0, 1, 1), POSE)   # cannot be encoded
    w.submit(scene(320, 240), T0 + 1, 2, "p1:2", 0.9, (0, 0, 1, 1), POSE)
    run_and_flush(w)
    assert w.failed == 1 and w.saved == 1
    assert [r["track_key"] for r in rows(w)] == ["p1:2"]


def test_photos_dir_does_not_disturb_snapshot_versions(tmp_path):
    from rescue_edge.snapshot import latest_version

    w = PhotoWriter(tmp_path, "m1")
    w.submit(scene(320, 240), T0, 1, "p1:1", 0.9, (0, 0, 1, 1), POSE)
    run_and_flush(w)
    assert latest_version(tmp_path / "m1") == 0       # 'photos/' must not look like a version dir


# ------------------------------------------------------------------ pipeline hook
def fake_pipeline(tmp_path, pass_id="p1"):
    return SimpleNamespace(photos=PhotoWriter(tmp_path, "m1"), agg=SimpleNamespace(pass_id=pass_id))


def test_pipeline_photographs_each_track_at_its_first_frame_only(tmp_path):
    pipe = fake_pipeline(tmp_path)
    look = SimpleNamespace(pose=POSE, reason=None)
    img = scene(320, 240)
    box = (10.0, 20.0, 60.0, 120.0)
    frames = [
        (10, T0, [Detection(box, 0.6, 5)]),
        (11, T0 + 0.04, [Detection(box, 0.7, 5), Detection(box, 0.5, 6)]),   # track 6 appears
        (12, T0 + 0.08, [Detection(box, 0.9, 5), Detection(box, 0.9, 6), Detection(box, 0.4, None)]),
    ]
    for index, t, dets in frames:
        Pipeline._snapshot_new_tracks(pipe, Frame(index, t, img, 320, 240), dets, look)
    run_and_flush(pipe.photos)

    got = {r["track_key"]: r for r in rows(pipe.photos)}
    assert set(got) == {"p1:5", "p1:6"}                        # untracked detection skipped
    assert got["p1:5"]["frame_index"] == "10" and got["p1:6"]["frame_index"] == "11"
    assert float(got["p1:6"]["frame_t_epoch"]) == pytest.approx(T0 + 0.04)
    assert got["p1:5"]["confidence"] == "0.6"                  # confidence of the FIRST detection


def test_pipeline_uses_current_pass_id_and_records_missing_pose(tmp_path):
    pipe = fake_pipeline(tmp_path, pass_id="p2")
    look = SimpleNamespace(pose=None, reason="telemetry_stale")
    Pipeline._snapshot_new_tracks(pipe, Frame(1, T0, scene(320, 240), 320, 240),
                                  [Detection((1.0, 2.0, 3.0, 4.0), 0.8, 9)], look)
    run_and_flush(pipe.photos)
    (r,) = rows(pipe.photos)
    assert r["track_key"] == "p2:9" and r["drone_pose_unavailable_reason"] == "telemetry_stale"
