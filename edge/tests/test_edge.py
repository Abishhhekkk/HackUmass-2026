import csv
import json
import math

import pytest

from rescue_edge.aggregate import Aggregator
from rescue_edge.geolocate import (CameraModel, ErrorModel, Pose, ground_to_pixel, latlon_to_offset,
                                   offset_to_latlon, pixel_to_ground)
from rescue_edge.scheduler import AdaptiveScheduler, SchedulerConfig
from rescue_edge.snapshot import SnapshotWriter, latest_version, sha256_bytes, snapshot_dir
from rescue_edge.telemetry import TelemetryBuffer, TelemetrySample

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


def test_no_interpolation_across_gap():
    b = TelemetryBuffer(max_gap_s=1.0, max_age_s=5)
    b.add(sample(10.0, lat=LAT))
    b.add(sample(13.0, lat=LAT + 0.001))
    assert b.at(10.5).pose.lat == LAT  # nearest, not interpolated


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
