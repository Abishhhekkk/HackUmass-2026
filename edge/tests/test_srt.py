import csv
import json
import math
from datetime import datetime, timezone

import pytest

from rescue_edge.geolocate import CameraModel, Pose, latlon_to_offset, offset_to_latlon, pixel_to_ground
from rescue_edge.srt import load_srt, parse_srt, srt_samples, start_from_srt

LAT, LON = 42.38925, -72.5283


def mini_block(i, offset_s, lat, lon, rel_alt=30.0, gimbal=None, date="2026-11-14 10:00:00.000"):
    def ts(s):
        return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{int(s % 60):02d},{int(round(s % 1 * 1000)):03d}"
    g = f" [gb_yaw: {gimbal[0]} gb_pitch: {gimbal[1]} gb_roll: {gimbal[2]}]" if gimbal else ""
    return (f"{i}\n{ts(offset_s)} --> {ts(offset_s + 0.033)}\n<font size=\"28\">FrameCnt: {i}, DiffTime: 33ms\n"
            f"{date}\n[iso: 100] [shutter: 1/1000.0] [fnum: 1.7] [ev: 0] [color_md: default] [focal_len: 24.00] "
            f"[latitude: {lat:.6f}] [longitude: {lon:.6f}] [rel_alt: {rel_alt:.3f} abs_alt: 412.500]{g} [ct: 5500] </font>\n")


def test_parses_the_four_dji_styles():
    mini = parse_srt(mini_block(1, 0.0, LAT, LON, 30.1, gimbal=(91.0, -90.0, 0.0)))[0]
    assert (mini.lat, mini.lon, mini.alt_agl, mini.alt_source) == (42.38925, -72.5283, 30.1, "rel_alt")
    assert (mini.gb_yaw, mini.gb_pitch, mini.gb_roll) == (91.0, -90.0, 0.0)
    assert mini.local_time == datetime(2026, 11, 14, 10, 0, 0)

    mavic2 = parse_srt("7\n00:00:06,006 --> 00:00:07,007\n[iso : 100] [shutter : 1/400.0] "
                       "[latitude : 42.389] [longtitude : -72.528] [altitude: 25.4]\n")[0]
    assert (mavic2.offset_s, mavic2.lat, mavic2.lon, mavic2.alt_agl) == (6.006, 42.389, -72.528, 25.4)
    assert mavic2.alt_source.startswith("altitude")

    phantom = parse_srt("1\n00:00:01,000 --> 00:00:02,000\nF/2.8, SS 320, ISO 100, EV 0, "
                        "GPS (-72.5283, 42.3890, 19), D 30.12m, H 18.50m, H.S 0.00m/s, V.S 0.00m/s\n")[0]
    assert (phantom.lat, phantom.lon, phantom.alt_agl, phantom.alt_source) == (42.389, -72.5283, 18.5, "H")

    old = parse_srt("1\n00:00:00,000 --> 00:00:01,000\nHOME(-72.5283,42.3890) 2017.08.12 14:05:11\n"
                    "GPS(-72.5284,42.3891,16) BAROMETER:31.2\n")[0]
    assert (old.lat, old.lon, old.alt_agl, old.alt_source) == (42.3891, -72.5284, 31.2, "BAROMETER")


def test_timing_is_video_position_and_start_comes_from_local_clock():
    recs = parse_srt(mini_block(1, 0.0, LAT, LON, date="2026-11-14 10:00:00.000")
                     + "\n" + mini_block(2, 2.5, LAT, LON, date="2026-11-14 10:00:02.500"))
    start = start_from_srt(recs, utc_offset_hours=-5)  # Eastern: 10:00 local = 15:00 UTC
    assert start == datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc).timestamp()
    samples = srt_samples(recs, start_time=1000.0)
    assert [s.t for s in samples] == [1000.0, 1002.5]


def test_heading_from_gimbal_else_direction_of_travel_else_unknown():
    gimbal = srt_samples(parse_srt(mini_block(1, 0, LAT, LON, gimbal=(-90.0, -90.0, 0.0))), 0.0)
    assert gimbal[0].hdg == 270.0 and gimbal[0].valid

    text = ""
    for i in range(12):  # fly east 1 m/s for 6 s, then hover for 6 s
        lat, lon = offset_to_latlon(LAT, LON, 0, float(min(i, 6)))
        text += mini_block(i + 1, i * 1.0, lat, lon) + "\n"
    s = srt_samples(parse_srt(text), 0.0, min_move_m=1.5, cog_hold_s=3.0)
    assert [x.invalid_reason for x in s[:2]] == ["heading_unknown", "heading_unknown"]  # < 1.5 m moved
    assert s[2].hdg == pytest.approx(90.0, abs=0.5) and s[2].valid            # 2 m east: heading east
    assert s[9].valid and not s[10].valid      # last move at t=6; heading held 3 s, then unknown
    assert s[10].invalid_reason == "heading_unknown"


def test_gimbal_tilt_rejects_oblique_footage_and_missing_gps():
    s = srt_samples(parse_srt(mini_block(1, 0, LAT, LON, gimbal=(0.0, -60.0, 0.0))
                              + "\n" + mini_block(2, 1, 0.0, 0.0)), 0.0)
    assert s[0].pitch == 30.0  # 30 deg forward of straight down
    cam = CameraModel.from_hfov(1920, 1080, 70)
    pose = Pose(s[0].lat, s[0].lon, s[0].alt_agl, s[0].hdg, s[0].pitch, s[0].roll)
    assert pixel_to_ground(960, 540, pose, cam).reason == "attitude_exceeds_limit"
    assert s[1].invalid_reason == "gnss_fix_invalid"  # DJI logs 0,0 with no GPS


def test_dfov_camera_model():
    cam = CameraModel.from_config({"width": 1920, "height": 1080, "dfov_deg": 82.1})
    hfov = 2 * math.degrees(math.atan(960 / cam.fx))
    assert hfov == pytest.approx(74.4, abs=0.1)  # 82.1 deg diagonal on 16:9 = 74.4 deg horizontal


def test_recorded_video_with_dji_srt_locates_person(tmp_path):
    """End to end: MP4 + its SRT (gimbal heading east) -> person located where they stand."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from rescue_edge.detector import Detection
    from rescue_edge.geolocate import ground_to_pixel
    from rescue_edge.main import Pipeline

    w, h, fps, n = 640, 360, 10, 30
    cam = CameraModel.from_config({"width": w, "height": h, "dfov_deg": 82.1})
    person_lat, person_lon = offset_to_latlon(LAT, LON, 3.0, 5.0)  # 3 m north, 5 m east of drone
    pose = Pose(LAT, LON, 30.0, 90.0, 0.0, 0.0)
    u, v = ground_to_pixel(person_lat, person_lon, pose, cam)

    clip = tmp_path / "DJI_0001.MP4"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for _ in range(n):
        frame = np.zeros((h, w, 3), np.uint8)
        frame[int(v) - 8:int(v), int(u) - 3:int(u) + 3] = 255  # feet at (u, v)
        writer.write(frame)
    writer.release()
    srt = tmp_path / "DJI_0001.SRT"
    srt.write_text("\n".join(mini_block(i + 1, i / fps, LAT, LON, 30.0, gimbal=(90.0, -90.0, 0.0),
                                        date=f"2026-11-14 10:00:0{i // fps}.{(i % fps) * 100:03d}")
                             for i in range(n)))

    cfg = {"mission_id": "srt-test", "storage_root": str(tmp_path / "snap"),
           "source": {"kind": "video", "path": str(clip), "video_start_time": None},
           "telemetry": {"kind": "srt", "path": str(srt), "utc_offset_hours": -5},
           "camera": {"width": w, "height": h, "dfov_deg": 82.1},
           "detector": {"kind": "mock"}, "scheduler": {"search_stride": 1}}
    pipe = Pipeline(cfg, tmp_path)
    assert pipe.cfg["source"]["video_start_time"] == datetime(2026, 11, 14, 15, 0, tzinfo=timezone.utc).timestamp()

    def bright_blob(frame):
        ys, xs = np.nonzero(frame.image[..., 0] > 128)
        return [Detection((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1), 0.9, 1)] if len(xs) else []

    pipe.detector = bright_blob
    pipe.run()
    people = pipe.agg.finalize().people
    assert len(people) == 1
    north, east = latlon_to_offset(person_lat, person_lon, people[0].lat, people[0].lon)
    assert math.hypot(north, east) < 0.5  # within half a metre of where they stand
    d = tmp_path / "snap" / "srt-test"
    manifest = json.loads(next(d.glob("v*/manifest.json")).read_text())
    assert manifest["data_window"]["start"] == "2026-11-14T15:00:00.000Z"
    rows = list(csv.DictReader(next(d.glob("v*/flight_path.csv")).open()))
    assert rows and rows[0]["valid"] == "1" and float(rows[0]["heading_deg"]) == 90.0


def test_srt_tool_csv_replays_identically(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from rescue_edge.telemetry import load_csv

    srt = tmp_path / "f.SRT"
    srt.write_text("\n".join(mini_block(i + 1, i * 1.0, *offset_to_latlon(LAT, LON, 0, 3.0 * i))
                             for i in range(6)))
    out = tmp_path / "tel.csv"
    repo = Path(__file__).resolve().parents[2]
    r = subprocess.run([sys.executable, str(repo / "scripts/srt_to_telemetry.py"), str(srt),
                        "--start", "2026-11-14T15:00:00Z", "--csv", str(out)],
                       capture_output=True, text=True, check=True)
    assert "video_start_time (UTC): 2026-11-14T15:00:00+00:00" in r.stdout
    assert "WARNING: no gimbal pitch" in r.stdout
    direct, _ = load_srt(srt, start_time=datetime(2026, 11, 14, 15, tzinfo=timezone.utc).timestamp())
    replayed = load_csv(out)
    for t in (direct._t[0], direct._t[3] + 0.4):
        a, b = direct.at(t), replayed.at(t)
        assert a.reason == b.reason
        if a.pose:
            assert (round(a.pose.lat, 6), round(a.pose.heading_deg, 1)) == (round(b.pose.lat, 6), round(b.pose.heading_deg, 1))
    assert direct.at(direct._t[0]).reason == "heading_unknown"  # first sample: no motion yet


def test_simulated_flight_log_and_config_run_through_pipeline(tmp_path):
    """scripts/simulate_telemetry.py: CSV + config for a clip without a real log; positions
    follow the made-up path, and the data is labelled as simulated."""
    import subprocess
    import sys
    from pathlib import Path

    import yaml

    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from rescue_edge.detector import Detection
    from rescue_edge.main import Pipeline, load_config

    repo = Path(__file__).resolve().parents[2]
    w, h, fps, n = 640, 360, 10, 30
    clip = tmp_path / "top down crowd.mp4"
    writer = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    for _ in range(n):
        frame = np.zeros((h, w, 3), np.uint8)
        frame[172:180, 317:323] = 255  # "person" whose feet are at the image centre
        writer.write(frame)
    writer.release()
    cfg_path = tmp_path / "config" / "clip.sim.yaml"
    csv_path = tmp_path / "tel.csv"
    r = subprocess.run([sys.executable, str(repo / "scripts/simulate_telemetry.py"), str(clip),
                        "--alt", "30", "--heading", "90", "--speed", "5", "--start", "2026-11-15T12:00:00Z",
                        "--out-csv", str(csv_path), "--config-out", str(cfg_path)],
                       capture_output=True, text=True, check=True)
    assert "640x360" in r.stdout and "DEMO" in r.stdout
    assert cfg_path.read_text().startswith("# GENERATED") and "SIMULATED" in cfg_path.read_text()

    cfg, base = load_config(str(cfg_path))
    assert cfg["mission_id"] == "sim-top-down-crowd" and cfg["camera"]["width"] == 640
    rows = list(csv.DictReader(csv_path.open()))
    start = datetime(2026, 11, 15, 12, tzinfo=timezone.utc).timestamp()
    assert float(rows[0]["t"]) <= start - 0.9 and float(rows[-1]["t"]) >= start + n / fps + 0.9

    cfg["source"]["realtime"] = False
    cfg["storage_root"] = str(tmp_path / "snap")
    cfg["uploader"]["enabled"] = False
    cfg["detector"] = {"kind": "mock"}
    cfg["scheduler"] = {"search_stride": 1}
    pipe = Pipeline(cfg, base)

    def bright_blob(frame):
        ys, xs = np.nonzero(frame.image[..., 0] > 128)
        return [Detection((xs.min(), ys.min(), xs.max() + 1, ys.max() + 1), 0.9, 1)] if len(xs) else []

    pipe.detector = bright_blob
    pipe.run()
    people = pipe.agg.finalize().people
    assert len(people) == 1
    north, east = latlon_to_offset(42.38925, -72.52830, people[0].lat, people[0].lon)
    assert abs(north) < 0.5 and 5.0 < east < 10.0  # under the drone flying east, median ~1.5 s in
    snap = next((tmp_path / "snap" / "sim-top-down-crowd").glob("v*"))
    assert json.loads((snap / "telemetry.json").read_text())["source"] == "SIMULATED flight path (not real GPS)"
    yaml.safe_load(cfg_path.read_text())  # generated file stays valid YAML
