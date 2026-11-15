"""Video recorder, timestamp matching, and the rule that media never leaves the device.

Free of fastapi, so it runs anywhere with OpenCV.
"""
import csv
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from rescue_edge import video as video_mod  # noqa: E402
from rescue_edge.detector import Detection  # noqa: E402
from rescue_edge.main import Pipeline  # noqa: E402
from rescue_edge.snapshot import latest_version  # noqa: E402
from rescue_edge.uploader import Uploader  # noqa: E402
from rescue_edge.video import VideoRecorder  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import match_media  # noqa: E402

T0 = 1794668400.0


def frame(i=0, w=640, h=360):
    return np.full((h, w, 3), (i * 3) % 255, np.uint8)


def recorder(tmp_path, **kw):
    args = dict(width=320, height=180, fps=15, segment_s=2, min_free_gb=0, queue_size=1000)
    args.update(kw)
    return VideoRecorder(tmp_path, "m1", **args)


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ video recorder
def test_segments_roll_over_and_frame_counts_agree(tmp_path):
    r = recorder(tmp_path)
    r.start()
    for i in range(150):                       # 5 s of a 30 fps camera
        r.submit(frame(i), T0 + i / 30, i)
    r.stop()

    index = rows(r.index_path)
    assert [x["start_utc"] for x in index] == ["2026-11-14T15:00:00.000Z", "2026-11-14T15:00:02.000Z",
                                               "2026-11-14T15:00:04.000Z"]
    assert sum(int(x["frames"]) for x in index) == r.written == r.accepted
    assert 70 <= r.written <= 80                # ~15 fps kept out of 30
    for x in index:
        video = r.dir / x["file"]
        frames_csv = rows(video.with_suffix(".frames.csv"))
        cap = cv2.VideoCapture(str(video))
        assert int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) == len(frames_csv) == int(x["frames"])
        assert (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))) == (320, 180)
        assert float(frames_csv[0]["frame_t_epoch"]) == pytest.approx(float(x["start_t_epoch"]))


def test_frame_log_holds_true_times_and_source_indexes(tmp_path):
    r = recorder(tmp_path, segment_s=60)
    r.start()
    for i in range(0, 30, 3):                  # irregular camera: every 3rd index, 0.1 s apart
        r.submit(frame(i), T0 + i / 30, 1000 + i)
    r.stop()
    (seg,) = sorted(r.dir.glob("seg_*.frames.csv"))
    logged = rows(seg)
    assert [int(x["source_frame_index"]) for x in logged] == [1000 + i for i in range(0, 30, 3)]
    assert [float(x["frame_t_epoch"]) for x in logged] == pytest.approx([T0 + i / 30 for i in range(0, 30, 3)])


def test_full_queue_never_blocks_detection(tmp_path):
    r = recorder(tmp_path, queue_size=1)       # writer thread not started: the queue stays full
    done = threading.Event()

    def feed():
        assert r.submit(frame(), T0, 0) is True
        assert r.submit(frame(), T0 + 1, 1) is False   # would block => skipped
        done.set()

    threading.Thread(target=feed, daemon=True).start()
    assert done.wait(2.0), "submit() blocked on a full queue"
    assert r.dropped == 1


def test_recording_stops_when_disk_is_nearly_full(tmp_path, monkeypatch):
    monkeypatch.setattr(video_mod.shutil, "disk_usage", lambda p: SimpleNamespace(free=100 * 1024 ** 2))
    r = recorder(tmp_path, min_free_gb=1)
    r.start()
    r.submit(frame(), T0, 0)
    r.stop()
    assert r.disk_full is True and r.segments == 0 and not list(r.dir.glob("seg_*"))
    assert r.submit(frame(), T0 + 5, 1) is False


def test_clock_jumping_backwards_does_not_freeze_recording(tmp_path):
    r = recorder(tmp_path, segment_s=60)
    r.start()
    assert r.submit(frame(), T0 + 100, 0) and r.submit(frame(), T0, 1)    # GPS clock corrected
    r.stop()
    assert r.segments == 2 and r.written == 2


def test_writer_failures_disable_recording_without_killing_anything(tmp_path, monkeypatch):
    r = recorder(tmp_path)

    def broken(base):
        raise RuntimeError("no writer")

    monkeypatch.setattr(r, "_make_writer", broken)
    r.start()
    for i in range(10):
        r.submit(frame(i), T0 + i, i)
    r.stop()
    assert r.disabled is True and r.failed == 5
    assert r.submit(frame(), T0 + 99, 99) is False


def test_h264_request_falls_back_to_mjpg_when_gstreamer_is_unusable(tmp_path, monkeypatch):
    monkeypatch.setattr(video_mod, "_opencv_has_gstreamer", lambda: True)   # claims yes, cannot deliver here
    r = recorder(tmp_path, codec="h264")
    r.start()
    r.submit(frame(), T0, 0)
    r.stop()
    if r.codec_used == "h264":
        pytest.skip("this OpenCV really has GStreamer H.264")
    assert r.codec_used == "mjpg" and r.written == 1


# ------------------------------------------------------------------ whole pipeline
@pytest.fixture
def mission(tmp_path):
    """Synthetic flight: 3 s of video, valid telemetry, three fake person tracks."""
    vid = tmp_path / "clip.avi"
    w = cv2.VideoWriter(str(vid), cv2.VideoWriter_fourcc(*"MJPG"), 30.0, (640, 360))
    for i in range(90):
        img = np.full((360, 640, 3), 40, np.uint8)
        cv2.putText(img, f"frame {i}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
        w.write(img)
    w.release()
    tel = tmp_path / "telemetry.csv"
    with open(tel, "w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop"])
        for k in range(40):
            cw.writerow([T0 + k * 0.1 - 0.5, 42.38925, -72.5283, 30, 90, 0, 0, 3, 0.9])
    cfg = {
        "mission_id": "m1", "drone_id": "drone-01", "storage_root": str(tmp_path / "snaps"),
        "source": {"kind": "video", "path": str(vid), "video_start_time": T0, "realtime": False},
        "telemetry": {"kind": "replay", "path": str(tel)},
        "camera": {"width": 640, "height": 360, "hfov_deg": 70.0},
        "detector": {"kind": "mock"},
        "scheduler": {"search_stride": 1, "active_stride": 1},
        "photos": {"enabled": True, "jpeg_quality": 90},
        "video": {"enabled": True, "width": 320, "height": 180, "fps": 15, "segment_s": 1,
                  "queue_size": 1000, "min_free_gb": 0},
        "snapshot_interval_s": 1.0,
    }
    pipe = Pipeline(cfg, tmp_path)
    box = (300.0, 150.0, 340.0, 250.0)

    def fake_detector(f):
        d = []
        if f.index >= 20:
            d.append(Detection(box, 0.8, 5))          # confirmed person, from frame 20
        if f.index >= 50:
            d.append(Detection(box, 0.7, 6))          # confirmed person, from frame 50
        if f.index in (80, 81):
            d.append(Detection(box, 0.5, 7))          # flicker: too short to count as a person
        return d

    pipe.detector = fake_detector
    pipe.run()
    return pipe, tmp_path / "snaps" / "m1"


def test_photo_and_video_match_back_to_a_location_by_time(mission):
    pipe, mdir = mission
    result = {r["track_key"]: r for r in match_media.match(mdir)}
    assert set(result) == {"p1:5", "p1:6", "p1:7"}

    for key, frame_index in (("p1:5", 20), ("p1:6", 50)):
        r = result[key]
        assert r["status"] == "estimated_sighting" and r["lat"] and r["lon"]
        assert abs(float(r["lat"]) - 42.38925) < 0.001 and abs(float(r["lon"]) + 72.5283) < 0.001
        assert r["drone_lat"] == "42.3892500"
        # the matched video frame is the recorded frame closest to the photo's time
        photo_t = T0 + frame_index / 30
        seg = mdir / "video" / Path(r["video_file"]).with_suffix(".frames.csv")
        logged = {int(x["frame_no"]): float(x["frame_t_epoch"]) for x in rows(seg)}
        assert abs(logged[int(r["video_frame_no"])] - photo_t) <= 1 / 15
    assert result["p1:7"]["status"] == "not_confirmed" and result["p1:7"]["lat"] == ""


def test_match_script_writes_csv(mission, capsys, monkeypatch):
    _, mdir = mission
    out = mdir / "media_matches.csv"
    monkeypatch.setattr(sys, "argv", ["match_media.py", str(mdir)])
    assert match_media.main() == 0
    assert out.exists() and len(rows(out)) == 3
    assert "2 matched to a located person" in capsys.readouterr().out


def test_media_is_never_part_of_an_upload(mission, monkeypatch):
    pipe, mdir = mission
    assert list((mdir / "photos").glob("*.jpg")) and list((mdir / "video").glob("seg_*"))   # media exists
    sent = {}

    def fake_post(url, files, headers, timeout):
        sent["parts"] = [(name, blob) for _field, (name, blob, *_r) in files]
        return SimpleNamespace(status_code=201, text="", json=lambda: {"status": "accepted"})

    monkeypatch.setattr("rescue_edge.uploader.requests.post", fake_post)
    Uploader(pipe.root, "m1", "http://cloud.invalid").upload_once(latest_version(mdir))

    names = {n for n, _ in sent["parts"]}
    assert names == {"manifest.json", "sightings.csv", "heatmap.geojson", "telemetry.json", "report.md"}
    assert not any(n.endswith((".jpg", ".avi", ".mkv", ".mp4")) or "frames" in n or "index" in n for n in names)
    payload = b"".join(blob for _, blob in sent["parts"])
    for jpg in (mdir / "photos").glob("*.jpg"):
        assert jpg.read_bytes()[:2000] not in payload             # not smuggled in under another name


def test_pipeline_status_reports_media(mission):
    pipe, _ = mission
    s = pipe.status()
    assert s["photos"]["saved"] == 3 and s["video"]["frames_written"] > 0 and s["video"]["disk_full"] is False


# ------------------------------------------------------------------ the live runner (edge_inference.py)
def test_edge_inference_runner_also_saves_photos_and_video(tmp_path, monkeypatch):
    """Runs the real main() of the live runner; only camera, serial port and YOLO are faked."""
    import json
    from datetime import datetime

    import edge_inference as ei
    from rescue_edge.capture import Frame
    from rescue_edge.telemetry import TelemetrySample

    engine = tmp_path / "m.engine"
    engine.write_bytes(b"x")
    cfg = {
        "mission_id": "live1", "storage_root": str(tmp_path / "snaps"),
        "source": {"kind": "camera", "device": 0},
        "telemetry": {"kind": "serial", "port": "/dev/null", "baud": 115200},
        "camera": {"width": 640, "height": 360, "hfov_deg": 70.0},
        "detector": {"kind": "yolo", "model_path": str(engine), "imgsz": 640, "conf_threshold": 0.35},
        "photos": {"enabled": True},
        "video": {"enabled": True, "width": 320, "height": 180, "fps": 15, "segment_s": 1,
                  "queue_size": 1000, "min_free_gb": 0},
    }
    import yaml
    cfg_path = tmp_path / "config" / "edge.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text(yaml.safe_dump(cfg))

    class FakeReader:
        port, last_error = "fake", None

        def __init__(self, port, baud, buf, log_path=None):
            for k in range(60):
                buf.add(TelemetrySample(T0 + k * 0.1 - 0.5, 42.38925, -72.5283, 30, 90, 0, 0, 3, 0.9))

        def now(self):
            return T0

        def start(self): pass
        def stop(self): pass
        def is_alive(self): return False
        def join(self, timeout=None): pass

    class FakeDetector:
        def __init__(self, **kw): pass

        def __call__(self, f):
            return [Detection((300.0, 150.0, 340.0, 250.0), 0.8, 5)] if f.index >= 10 else []

    frames = [Frame(i, T0 + i / 30, frame(i), 640, 360) for i in range(60)]
    monkeypatch.setattr(ei, "SerialTelemetryReader", FakeReader)
    monkeypatch.setattr(ei, "YoloTracker", FakeDetector)
    monkeypatch.setattr(ei, "_camera_source", lambda c, clock: iter(frames))
    out = tmp_path / "det.jsonl"
    monkeypatch.setattr(sys, "argv", ["edge_inference.py", "--config", str(cfg_path), "--output", str(out)])
    assert ei.main() == 0

    records = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(records) == 50 and records[0]["target_lat"] is not None   # documented output unchanged
    assert set(records[0]) == {"object_class", "confidence", "target_lat", "target_lon", "timestamp",
                               "horizontal_error_m", "position_method", "unlocated_reason"}

    mdir = tmp_path / "snaps" / "live1"
    (photo,) = rows(mdir / "photos" / "photos_index.csv")
    assert photo["track_key"].endswith(":5") and photo["frame_index"] == "10"
    assert photo["drone_lat"] == "42.3892500"
    # the documented way to match without sightings.csv: photo time == the detection record's timestamp
    t_photo = datetime.fromisoformat(photo["frame_t_utc"].replace("Z", "+00:00"))
    assert t_photo == datetime.fromisoformat(records[0]["timestamp"])
    assert list((mdir / "video").glob("seg_*.frames.csv")) and (mdir / "video" / "video_index.csv").exists()
