import csv
import json
import math
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from edge_inference import _detection_record
from rescue_edge.aggregate import Aggregator
from rescue_edge.capture import Frame
from rescue_edge.detector import Detection
from rescue_edge.geolocate import (CameraModel, ErrorModel, Pose, ground_to_pixel, latlon_to_offset,
                                   offset_to_latlon, pixel_to_ground)
from rescue_edge.local_server import create_app
from rescue_edge.scheduler import AdaptiveScheduler, SchedulerConfig
from rescue_edge.snapshot import SnapshotWriter, latest_version, sha256_bytes, snapshot_dir
from rescue_edge.telemetry import SerialTelemetryReader, TelemetryBuffer, TelemetrySample

CAM = CameraModel.from_hfov(1920, 1080, 70.0)
LAT, LON = 42.3893, -72.5283


def offset(res, pose=None):
    p = pose or Pose(LAT, LON, 30, 0)
    return latlon_to_offset(p.lat, p.lon, res.lat, res.lon)


# ------------------------------------------------------------------ geolocation
def test_centre_pixel_is_below_drone():
    r = pixel_to_ground(CAM.cx, CAM.cy, Pose(LAT, LON, 30, 0), CAM)
    n, e = offset(r)
    assert abs(n) < 1e-6 and abs(e) < 1e-6
    assert r.horizontal_error_m > 2.5  # at least the GNSS error


@pytest.mark.parametrize("du,dv,heading,exp_n,exp_e", [
    (+400, 0, 0, 0, +1),     # right of image, nose north -> east
    (-400, 0, 0, 0, -1),     # left -> west
    (0, -300, 0, +1, 0),     # top of image = nose = north
    (0, +300, 0, -1, 0),     # bottom -> south
    (+400, 0, 90, -1, 0),    # nose east: right of image -> south
    (0, -300, 90, 0, +1),    # nose east: top of image -> east
])
def test_directions(du, dv, heading, exp_n, exp_e):
    pose = Pose(LAT, LON, 30, heading)
    n, e = offset(pixel_to_ground(CAM.cx + du, CAM.cy + dv, pose, CAM), pose)
    assert math.copysign(1, n) == exp_n if exp_n else abs(n) < 1e-6
    assert math.copysign(1, e) == exp_e if exp_e else abs(e) < 1e-6


def test_ground_distance_scales_with_altitude():
    d = []
    for alt in (20, 40):
        pose = Pose(LAT, LON, alt, 0)
        n, e = offset(pixel_to_ground(CAM.cx + 500, CAM.cy, pose, CAM), pose)
        d.append(math.hypot(n, e))
    assert d[1] == pytest.approx(2 * d[0], rel=1e-6)


def test_roundtrip_with_attitude():
    pose = Pose(LAT, LON, 25, 137, pitch_deg=4, roll_deg=-6)
    tlat, tlon = offset_to_latlon(LAT, LON, 6.0, -4.0)
    u, v = ground_to_pixel(tlat, tlon, pose, CAM)
    r = pixel_to_ground(u, v, pose, CAM)
    n, e = latlon_to_offset(tlat, tlon, r.lat, r.lon)
    assert math.hypot(n, e) < 0.01


def test_invalid_inputs_return_null_with_reason():
    assert pixel_to_ground(960, 540, Pose(LAT, LON, 0.2, 0), CAM).reason == "altitude_invalid"
    assert pixel_to_ground(960, 540, Pose(LAT, LON, 30, 0, pitch_deg=35), CAM).reason == "attitude_exceeds_limit"
    r = pixel_to_ground(960, 540, Pose(LAT, LON, 30, 0, roll_deg=19), CAM, ErrorModel(max_off_nadir_deg=10))
    assert r.lat is None and r.reason == "ray_too_oblique"


# ------------------------------------------------------------------ telemetry
def sample(t, **kw):
    d = dict(t=t, lat=LAT, lon=LON, alt_agl=30, hdg=0, pitch=0, roll=0, fix=3, hdop=1)
    d.update(kw)
    return TelemetrySample(**d)


def test_interpolation_and_heading_wrap():
    b = TelemetryBuffer()
    b.add(sample(10.0, lat=LAT, hdg=350))
    b.add(sample(10.2, lat=LAT + 0.0001, hdg=10))
    p = b.at(10.1).pose
    assert p.lat == pytest.approx(LAT + 0.00005)
    assert p.heading_deg == pytest.approx(0, abs=1e-6) or p.heading_deg == pytest.approx(360)


def test_stale_and_invalid_fix():
    b = TelemetryBuffer(max_age_s=2.0)
    b.add(sample(10.0))
    b.add(sample(11.0, fix=0))
    assert b.at(20.0).reason == "telemetry_stale"
    assert b.at(11.0).reason == "gnss_fix_invalid"
    assert TelemetryBuffer().at(1).reason == "no_telemetry"
    b.add(sample(12.0, alt_agl=-1))
    assert b.at(12.0).reason == "altitude_invalid"


def test_no_interpolation_across_gap():
    b = TelemetryBuffer(max_gap_s=1.0, max_age_s=5)
    b.add(sample(10.0, lat=LAT))
    b.add(sample(13.0, lat=LAT + 0.001))
    assert b.at(10.5).pose.lat == LAT  # nearest, not interpolated


def test_telemetry_buffer_is_bounded():
    b = TelemetryBuffer(max_samples=3)
    for t in range(5):
        b.add(sample(float(t)))
    assert len(b) == 3
    assert b.at(-1.0).reason == "telemetry_stale"
    assert b.at(4.0).pose.lat == LAT


def test_unbounded_buffer_keeps_every_sample():
    b = TelemetryBuffer(max_samples=None)
    for t in range(2000):
        b.add(sample(t * 0.1))
    assert len(b) == 2000
    assert b.at(0.05).pose is not None


def test_replayed_telemetry_log_is_not_truncated(tmp_path):
    """Regression: a 512-sample cap dropped the start of replayed flights, so every
    early frame came back telemetry_stale and the map showed no located people."""
    from rescue_edge.main import Pipeline

    n = 2000  # 200 s at 10 Hz, well over the live-feed cap
    tel = tmp_path / "telemetry.csv"
    with tel.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop"])
        for i in range(n):
            w.writerow([1000 + i * 0.1, LAT, LON, 30, 0, 0, 0, 3, 1])
    cfg = {
        "mission_id": "replay-test", "storage_root": str(tmp_path / "snap"),
        "source": {"kind": "replay", "path": str(tmp_path / "unused.jsonl")},
        "telemetry": {"kind": "replay", "path": str(tel)},
        "camera": {"width": 1920, "height": 1080, "hfov_deg": 70.0},
        "detector": {"kind": "mock"},
    }
    pipe = Pipeline(cfg, tmp_path)
    assert len(pipe.telemetry) == n
    assert pipe.telemetry.at(1000.05).pose is not None  # start of the flight


def _run_replay_pipeline(tmp_path, frame_w, frame_h):
    from rescue_edge.main import Pipeline

    tel = tmp_path / "telemetry.csv"
    with tel.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop"])
        for i in range(20):
            w.writerow([1000 + i * 0.1, LAT, LON, 30, 0, 0, 0, 3, 1])
    det = tmp_path / "detections.jsonl"
    with det.open("w") as f:
        for i in range(5):
            f.write(json.dumps({"frame": i, "t": 1000 + i * 0.1, "width": frame_w, "height": frame_h,
                                "detections": [{"bbox": [100, 100, 120, 140], "conf": 0.9,
                                                "track_id": 1}]}) + "\n")
    cfg = {
        "mission_id": "res-test", "storage_root": str(tmp_path / "snap"),
        "source": {"kind": "replay", "path": str(det)},
        "telemetry": {"kind": "replay", "path": str(tel)},
        "camera": {"width": 1920, "height": 1080, "hfov_deg": 70.0},
        "detector": {"kind": "mock"},
        "scheduler": {"search_stride": 1},
    }
    pipe = Pipeline(cfg, tmp_path)
    pipe.run()
    return pipe.agg.finalize()


def test_matching_resolution_is_located(tmp_path):
    state = _run_replay_pipeline(tmp_path, 1920, 1080)
    assert len(state.people) == 1 and not state.unlocated


def test_resolution_mismatch_keeps_sighting_unlocated(tmp_path):
    state = _run_replay_pipeline(tmp_path, 1280, 720)
    assert not state.people
    assert [t.unlocated_reason for t in state.unlocated] == ["camera_resolution_mismatch"]


def test_telemetry_parser_rejects_nonfinite_and_out_of_range_gps():
    with pytest.raises(ValueError, match="finite"):
        TelemetrySample.from_dict({"t": float("nan"), "lat": LAT, "lon": LON, "alt_agl": 10, "hdg": 0})
    with pytest.raises(ValueError, match="WGS84"):
        TelemetrySample.from_dict({"t": 1, "lat": 91, "lon": LON, "alt_agl": 10, "hdg": 0})


def test_serial_reader_stops_and_joins(monkeypatch):
    connected = threading.Event()
    payload = (json.dumps({
        "t": time.time(), "lat": LAT, "lon": LON, "alt_agl": 10,
        "hdg": 0, "pitch": 0, "roll": 0, "fix": 3, "sats": 9, "hdop": 1,
    }) + "\n").encode()

    class FakeSerial:
        def __init__(self, _port, _baud, timeout):
            assert timeout > 0
            self.is_open = True
            self._payload = payload
            connected.set()

        def readline(self):
            if self._payload:
                line, self._payload = self._payload, b""
                return line
            time.sleep(0.005)
            return b""

        def close(self):
            self.is_open = False

    monkeypatch.setitem(
        sys.modules, "serial", SimpleNamespace(Serial=FakeSerial, SerialException=OSError)
    )
    buffer = TelemetryBuffer()
    reader = SerialTelemetryReader("/dev/test", 115200, buffer)
    reader.start()
    assert connected.wait(timeout=1)
    deadline = time.monotonic() + 1
    while len(buffer) == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    reader.stop()
    reader.join(timeout=1)
    assert not reader.is_alive()
    assert len(buffer) == 1


def test_live_detection_json_uses_geolocation_and_rejects_resolution_mismatch():
    pose_buffer = TelemetryBuffer()
    pose_buffer.add(sample(100.0))
    detection = Detection((950, 530, 970, 550), 0.84, None)
    frame = Frame(0, 100.0, width=CAM.width, height=CAM.height)
    result = _detection_record(frame, detection, pose_buffer, CAM, ErrorModel(), "foot")
    assert result["object_class"] == "person"
    assert result["confidence"] == pytest.approx(0.84)
    assert result["target_lat"] == pytest.approx(LAT)
    assert result["target_lon"] == pytest.approx(LON)
    assert result["unlocated_reason"] is None

    wrong_size = Frame(1, 100.0, width=1280, height=720)
    result = _detection_record(wrong_size, detection, pose_buffer, CAM, ErrorModel(), "foot")
    assert result["target_lat"] is None and result["target_lon"] is None
    assert result["unlocated_reason"] == "camera_resolution_mismatch"


# ------------------------------------------------------------------ aggregation
def add_track(agg, tid, t0, n_frames, lat, lon, err=3.0):
    for i in range(n_frames):
        agg.add(t0 + i * 0.1, tid, 0.8, lat, lon, err, None)
        agg.note_frame(t0 + i * 0.1)


def test_same_person_on_two_passes_is_merged():
    agg = Aggregator(merge_radius_m=4)
    add_track(agg, 1, 0, 10, LAT, LON)
    plat, plon = offset_to_latlon(LAT, LON, 1.5, 1.0)
    add_track(agg, 7, 60, 10, plat, plon)  # second pass, new track ID, 1.8 m away
    s = agg.finalize()
    assert len(s.people) == 1 and len(s.people[0].tracks) == 2


def test_simultaneous_tracks_are_never_merged():
    agg = Aggregator(merge_radius_m=4)
    add_track(agg, 1, 0, 10, LAT, LON)
    plat, plon = offset_to_latlon(LAT, LON, 1.0, 0)
    add_track(agg, 2, 0.2, 10, plat, plon)  # visible at the same time -> two people
    assert len(agg.finalize().people) == 2


def test_short_tracks_dropped_and_unlocated_kept():
    agg = Aggregator(min_frames=3)
    add_track(agg, 1, 0, 2, LAT, LON)  # noise
    for i in range(5):
        agg.add(i * 0.1, 9, 0.7, None, None, None, "gnss_fix_invalid")
    s = agg.finalize()
    assert s.people == [] and len(s.unlocated) == 1
    assert s.unlocated[0].unlocated_reason == "gnss_fix_invalid"


def test_grid_counts():
    agg = Aggregator(cell_size_m=10, merge_radius_m=1)
    add_track(agg, 1, 0, 5, LAT, LON)
    a = offset_to_latlon(LAT, LON, 3, 3)
    add_track(agg, 2, 10, 5, *a)
    b = offset_to_latlon(LAT, LON, 25, 3)
    add_track(agg, 3, 20, 5, *b)
    s = agg.finalize()
    counts = sorted(c.count for c in s.cells)
    assert counts == [1, 2]


# ------------------------------------------------------------------ snapshots
def test_snapshot_versions_and_checksums(tmp_path):
    agg = Aggregator()
    add_track(agg, 1, 0, 5, LAT, LON)
    w = SnapshotWriter(tmp_path, "m1", "drone-01")
    assert w.write_if_changed(agg.finalize()) == 1
    assert w.write_if_changed(agg.finalize()) is None  # unchanged -> no new version
    b = offset_to_latlon(LAT, LON, 30, 0)
    add_track(agg, 2, 10, 5, *b)
    assert w.write_if_changed(agg.finalize()) == 2
    d = snapshot_dir(tmp_path, "m1", 2)
    m = json.loads((d / "manifest.json").read_text())
    for f in m["files"]:
        assert sha256_bytes((d / f["name"]).read_bytes()) == f["sha256"]
    rows = list(csv.DictReader(open(d / "sightings.csv")))
    assert m["summary"]["estimated_people"] == 2 == len(rows)
    assert latest_version(tmp_path / "m1") == 2
    # a restarted writer continues numbering and remembers the last content
    assert SnapshotWriter(tmp_path, "m1", "drone-01").write_if_changed(agg.finalize()) is None


def test_telemetry_is_checksummed_and_changes_snapshot_content(tmp_path):
    agg = Aggregator()
    add_track(agg, 1, 0, 5, LAT, LON)
    writer = SnapshotWriter(tmp_path, "m1", "drone-01")
    pose = {"schema_version": 1, "available": True, "observed_at": "2026-01-01T00:00:00Z",
            "position": {"lat": LAT, "lon": LON}, "source": "edge_telemetry"}
    assert writer.write_if_changed(agg.finalize(), telemetry=pose) == 1
    folder = snapshot_dir(tmp_path, "m1", 1)
    assert json.loads((folder / "telemetry.json").read_text())["position"]["lat"] == LAT
    manifest = json.loads((folder / "manifest.json").read_text())
    entry = next(item for item in manifest["files"] if item["name"] == "telemetry.json")
    assert sha256_bytes((folder / "telemetry.json").read_bytes()) == entry["sha256"]
    changed_pose = {**pose, "position": {"lat": LAT + 0.0001, "lon": LON}}
    assert writer.write_if_changed(agg.finalize(), telemetry=changed_pose) == 2


def test_scheduler_strides():
    s = AdaptiveScheduler(SchedulerConfig(search_stride=4, hold_active_s=1), temp_reader=lambda: 50.0)
    processed = [i for i in range(12) if s.should_process(i)]
    assert processed == [0, 4, 8]
    s.update(t=100.0, n_detections=2)
    assert s.mode == "active" and s.stride == 1
    s.update(t=102.0, n_detections=0)
    assert s.mode == "search"
    hot = AdaptiveScheduler(SchedulerConfig(thermal_limit_c=80), temp_reader=lambda: 85.0)
    hot.update(0, 3)
    assert hot.mode == "thermal"


def test_network_demo_control_updates_uplink_state(tmp_path):
    class Uploader:
        def __init__(self):
            self.offline_flag = tmp_path / "OFFLINE"
            self.online = True
            self.poked = False

        def poke(self):
            self.poked = True

    class Pipe:
        root = tmp_path / "snapshots"
        uploader = Uploader()

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    pipe = Pipe()
    client = TestClient(create_app(pipe, web_dir))

    response = client.post("/api/v1/network", json={"offline": True})
    assert response.status_code == 200
    assert pipe.uploader.online is False
    assert pipe.uploader.offline_flag.exists()

    response = client.post("/api/v1/network", json={"offline": False})
    assert response.status_code == 200
    assert pipe.uploader.online is None
    assert not pipe.uploader.offline_flag.exists()
    assert pipe.uploader.poked


def test_edge_dashboard_explains_missing_build_then_serves_it(tmp_path):
    class Pipe:
        root = tmp_path / "snapshots"
        uploader = None

    web_dir = tmp_path / "web"
    web_dir.mkdir()
    (web_dir / "index.html").write_text('<script type="module" src="/src/main.jsx"></script>')
    client = TestClient(create_app(Pipe(), web_dir))
    r = client.get("/")
    assert r.status_code == 503 and "npm run build" in r.text  # not the raw, unrunnable source

    (web_dir / "dist").mkdir()
    (web_dir / "dist" / "index.html").write_text("<p>built dashboard</p>")
    r = client.get("/")
    assert r.status_code == 200 and "built dashboard" in r.text  # no restart needed


def test_recorded_video_runs_through_pipeline(tmp_path):
    """The hackathon path: a recorded clip + replayed telemetry -> located person in a snapshot.
    Uses a tiny generated clip and a stand-in detector, so no GPU/Ultralytics is needed."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from rescue_edge.main import Pipeline

    w, h, fps, n, start = 640, 360, 10, 30, 2000.0
    clip = tmp_path / "clip.mp4"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert writer.isOpened(), "OpenCV cannot write mp4v here"
    for i in range(n):
        writer.write(np.full((h, w, 3), i * 5, np.uint8))
    writer.release()

    tel = tmp_path / "telemetry.csv"
    with tel.open("w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop"])
        for i in range(40):
            cw.writerow([start - 0.5 + i * 0.1, LAT, LON, 30, 0, 0, 0, 3, 1])

    cfg = {
        "mission_id": "video-test", "storage_root": str(tmp_path / "snap"),
        "source": {"kind": "video", "path": str(clip), "video_start_time": "1970-01-01T00:33:20Z"},
        "telemetry": {"kind": "replay", "path": str(tel)},
        "camera": {"width": w, "height": h, "hfov_deg": 70.0},
        "detector": {"kind": "mock"},
        "scheduler": {"search_stride": 1},
    }
    pipe = Pipeline(cfg, tmp_path)
    seen = []

    def fake_detector(frame):  # person standing at the image centre, feet at the centre pixel
        seen.append((frame.index, frame.t, frame.image.shape))
        return [Detection((w / 2 - 5, h / 2 - 20, w / 2 + 5, h / 2), 0.9, 1)]

    pipe.detector = fake_detector
    pipe.run()

    assert len(seen) == n and seen[0][2] == (h, w, 3)
    assert [round(t - start, 3) for _, t, _ in seen] == [round(i / fps, 3) for i in range(n)]
    state = pipe.agg.finalize()
    assert state.frames_processed == n and not state.unlocated
    assert len(state.people) == 1
    north, east = latlon_to_offset(LAT, LON, state.people[0].lat, state.people[0].lon)
    assert math.hypot(north, east) < 0.1  # nadir camera, level drone: centre pixel is below it
    assert latest_version(tmp_path / "snap" / "video-test") >= 1


@pytest.mark.skipif(not __import__("os").environ.get("RESCUE_YOLO_WEIGHTS"),
                    reason="set RESCUE_YOLO_WEIGHTS=path/to/yolov8n.pt or .engine to run")
def test_real_yolo_through_pipeline(tmp_path):
    """Opt-in smoke test of the REAL detector + ByteTrack + pipeline (no mocks).

        RESCUE_YOLO_WEIGHTS=models/yolov8n.engine RESCUE_YOLO_DEVICE=0 python -m pytest -k real_yolo -s

    Uses Ultralytics' bundled bus.jpg (ground-level, 4 people) as a short panning clip.
    It proves the code path and prints ms/frame; it says nothing about aerial accuracy.
    """
    import os
    from pathlib import Path

    ultralytics = pytest.importorskip("ultralytics")
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from rescue_edge.main import Pipeline

    img = cv2.imread(str(Path(ultralytics.__file__).parent / "assets" / "bus.jpg"))
    h, w = img.shape[:2]
    clip = tmp_path / "bus.mp4"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 10, (w, h))
    for i in range(30):
        shift = np.float32([[1, 0, i * 2], [0, 1, 0]])
        writer.write(cv2.warpAffine(img, shift, (w, h), borderMode=cv2.BORDER_REPLICATE))
    writer.release()
    start = 1_794_668_400.0
    tel = tmp_path / "tel.csv"
    with tel.open("w", newline="") as f:
        cw = csv.writer(f)
        cw.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop"])
        for i in range(50):
            cw.writerow([start - 1 + i * 0.1, LAT, LON, 30, 0, 0, 0, 3, 1])
    weights = os.environ["RESCUE_YOLO_WEIGHTS"]
    cfg = {
        "mission_id": "real-yolo", "storage_root": str(tmp_path / "snap"),
        "source": {"kind": "video", "path": str(clip), "video_start_time": start},
        "telemetry": {"kind": "replay", "path": str(tel)},
        "camera": {"width": w, "height": h, "hfov_deg": 70.0},
        "detector": {"kind": "yolo", "model_path": weights,
                     "imgsz": int(os.environ.get("RESCUE_YOLO_IMGSZ", 640)),
                     "device": os.environ.get("RESCUE_YOLO_DEVICE", "cpu"),
                     # RESCUE_YOLO_TILE=320 exercises tiled detection + ByteTrack on merged boxes
                     "tile": int(os.environ.get("RESCUE_YOLO_TILE", 0)) or None},
        "scheduler": {"search_stride": 1},
    }
    pipe = Pipeline(cfg, tmp_path)
    timings, real = [], pipe.detector

    def timed(frame):
        t0 = time.perf_counter()
        out = real(frame)
        timings.append(time.perf_counter() - t0)
        return out

    pipe.detector = timed
    pipe.run()
    state = pipe.agg.finalize()
    steady = sorted(timings[3:])
    print(f"\nreal YOLO{' tiled ' + str(cfg['detector']['tile']) if cfg['detector']['tile'] else ''}: "
          f"{len(state.people)} estimated people, "
          f"median {1000 * steady[len(steady) // 2]:.0f} ms/frame on {cfg['detector']['device']}")
    assert pipe.agg.detections > 0 and pipe.agg.untracked < pipe.agg.detections
    assert len(state.people) >= 2  # bus.jpg has 4 people; some may merge or be too brief


def test_pipeline_records_flight_path_in_snapshot(tmp_path):
    from rescue_edge.main import Pipeline

    tel = tmp_path / "telemetry.csv"
    with tel.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t", "lat", "lon", "alt_agl", "hdg", "pitch", "roll", "fix", "hdop"])
        for i in range(60):
            w.writerow([1000 + i * 0.1, LAT, LON, 30, 0, 0, 0, 0 if 15 <= i <= 35 else 3, 1])
    det = tmp_path / "detections.jsonl"
    with det.open("w") as f:
        for i in range(50):  # 5 s of frames at 10 fps
            f.write(json.dumps({"frame": i, "t": 1000 + i * 0.1, "width": 1920, "height": 1080,
                                "detections": []}) + "\n")
    cfg = {"mission_id": "fp", "storage_root": str(tmp_path / "snap"),
           "source": {"kind": "replay", "path": str(det)},
           "telemetry": {"kind": "replay", "path": str(tel)},
           "camera": {"width": 1920, "height": 1080, "hfov_deg": 70.0}, "detector": {"kind": "mock"}}
    pipe = Pipeline(cfg, tmp_path)
    pipe.run()
    d = snapshot_dir(tmp_path / "snap", "fp", latest_version(tmp_path / "snap" / "fp"))
    rows = list(csv.DictReader((d / "flight_path.csv").open()))
    assert [round(float(r["t"]) - 1000, 1) for r in rows] == [0.0, 1.0, 2.0, 3.0, 4.0]  # 1 Hz
    assert [r["valid"] for r in rows] == ["1", "1", "0", "0", "1"]
    assert rows[2]["reason"] == "gnss_fix_invalid" and rows[2]["lat"] == ""
    manifest = json.loads((d / "manifest.json").read_text())
    assert "flight_path.csv" in {f["name"] for f in manifest["files"]}


def test_edge_flight_path_route_matches_cloud_shape(tmp_path):
    from rescue_edge.geolocate import Pose
    from rescue_edge.telemetry import FlightPathRecorder, PoseLookup

    rec = FlightPathRecorder(1.0)
    for i in range(4):
        rec.record(1000.0 + i, PoseLookup(Pose(LAT + i * 1e-5, LON, 30, 0)) if i != 2
                   else PoseLookup(None, "telemetry_stale"))
    agg = Aggregator()
    agg.note_frame(1000.0)
    SnapshotWriter(tmp_path / "snapshots", "m1", "drone-01").write_if_changed(
        agg.finalize(), flight_path=rec.rows())

    class Pipe:
        root = tmp_path / "snapshots"
        uploader = None

    client = TestClient(create_app(Pipe(), tmp_path / "web"))
    fc = client.get("/api/v1/missions/m1/flight-path").json()
    assert fc["properties"]["samples"] == 4 and fc["properties"]["gap_samples"] == 1
    assert [len(f["geometry"]["coordinates"]) for f in fc["features"]] == [2]  # 1-point tail dropped


def test_detector_model_path_is_relative_to_repo_root(tmp_path, monkeypatch):
    """Regression: models/yolov8n.engine failed when the pipeline was started from edge/."""
    import rescue_edge.main as mainmod

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "my.engine").write_bytes(b"x")
    seen = []
    monkeypatch.setattr(mainmod, "build_detector", lambda d: seen.append(d["model_path"]) or (lambda f: []))
    base_cfg = {"mission_id": "m", "storage_root": str(tmp_path / "snap"),
                "source": {"kind": "replay", "path": "unused"},
                "telemetry": {"kind": "replay", "path": str(tmp_path / "t.csv")},
                "camera": {"width": 10, "height": 10, "hfov_deg": 60}}
    (tmp_path / "t.csv").write_text("t,lat,lon,alt_agl,hdg\n")
    monkeypatch.chdir(tmp_path / "models")  # started from some other folder
    mainmod.Pipeline({**base_cfg, "detector": {"kind": "yolo", "model_path": "models/my.engine"}}, tmp_path)
    mainmod.Pipeline({**base_cfg, "detector": {"kind": "yolo", "model_path": "yolov8n.pt"}}, tmp_path)
    assert seen == [str(tmp_path / "models" / "my.engine"), "yolov8n.pt"]


def test_serve_only_viewer_serves_saved_snapshots(tmp_path):
    from rescue_edge.main import SnapshotViewer

    agg = Aggregator()
    agg.note_frame(1000.0)
    SnapshotWriter(tmp_path / "data" / "snap", "sim-crowd", "drone-sim").write_if_changed(agg.finalize())
    viewer = SnapshotViewer({"storage_root": "data/snap", "mission_id": "sim-crowd"}, tmp_path)
    client = TestClient(create_app(viewer, tmp_path / "web"))
    assert client.get("/api/v1/status").json()["serve_only"] is True
    assert [m["mission_id"] for m in client.get("/api/v1/missions").json()] == ["sim-crowd"]
    assert client.get("/api/v1/missions/sim-crowd/latest").status_code == 200
