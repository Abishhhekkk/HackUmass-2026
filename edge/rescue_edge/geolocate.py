"""Pixel -> ground coordinate for a downward-facing camera over flat ground.

Frame conventions (documented in docs/data-contract.md):

* Image:  u to the right, v downward, origin at top-left pixel.
* Camera: x right, y down (image axes), z out of the lens. Mounted nadir, with the
  TOP of the image pointing toward the drone's nose.
* Body:   x forward (nose), y right, z down  (FRD).
* World:  local NED tangent plane centred on the drone's GNSS fix.
* Attitude: heading (yaw) clockwise from true north, pitch nose-up positive,
  roll right-wing-down positive, applied Z-Y-X (yaw, pitch, roll).

Assumptions (method ``nadir_flat_ground_v1``): ground is a flat plane
``altitude_agl_m`` below the camera, the camera sits at the GNSS antenna, lens
distortion is negligible or already removed. Every one of these adds error,
which is why we return an explicit ``horizontal_error_m``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EARTH_RADIUS_M = 6_378_137.0
METHOD = "nadir_flat_ground_v1"


@dataclass(frozen=True)
class CameraModel:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float

    @classmethod
    def from_hfov(cls, width: int, height: int, hfov_deg: float) -> "CameraModel":
        """Rough model from horizontal field of view (use real calibration when you have it)."""
        fx = (width / 2) / math.tan(math.radians(hfov_deg) / 2)
        return cls(width, height, fx, fx, width / 2, height / 2)

    @classmethod
    def from_config(cls, cfg: dict) -> "CameraModel":
        if all(k in cfg for k in ("fx", "fy", "cx", "cy")):
            return cls(cfg["width"], cfg["height"], cfg["fx"], cfg["fy"], cfg["cx"], cfg["cy"])
        return cls.from_hfov(cfg["width"], cfg["height"], cfg["hfov_deg"])


@dataclass(frozen=True)
class Pose:
    lat: float
    lon: float
    altitude_agl_m: float
    heading_deg: float
    pitch_deg: float = 0.0
    roll_deg: float = 0.0


@dataclass(frozen=True)
class ErrorModel:
    """1-sigma-ish inputs for a conservative horizontal error estimate."""
    gnss_m: float = 2.5          # NEO-M8N class receiver, open sky
    altitude_m: float = 1.5      # GPS-relative altitude; use a rangefinder to shrink
    attitude_deg: float = 2.0    # heading/pitch/roll uncertainty combined
    foot_point_m: float = 0.7    # bbox bottom-centre vs. actual feet
    max_tilt_deg: float = 20.0   # reject frames with more pitch/roll than this
    max_off_nadir_deg: float = 55.0


@dataclass(frozen=True)
class GeoResult:
    lat: float | None
    lon: float | None
    horizontal_error_m: float | None
    method: str
    reason: str | None = None  # set when lat/lon is None

    @property
    def ok(self) -> bool:
        return self.lat is not None


def _rot_body_to_ned(heading_deg: float, pitch_deg: float, roll_deg: float) -> np.ndarray:
    y, p, r = (math.radians(a) for a in (heading_deg, pitch_deg, roll_deg))
    rz = np.array([[math.cos(y), -math.sin(y), 0], [math.sin(y), math.cos(y), 0], [0, 0, 1]])
    ry = np.array([[math.cos(p), 0, math.sin(p)], [0, 1, 0], [-math.sin(p), 0, math.cos(p)]])
    rx = np.array([[1, 0, 0], [0, math.cos(r), -math.sin(r)], [0, math.sin(r), math.cos(r)]])
    return rz @ ry @ rx


# camera (x right, y down, z out) -> body (x fwd, y right, z down) for a nadir mount, image top = nose
_CAM_TO_BODY = np.array([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)


def offset_to_latlon(lat0: float, lon0: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Local tangent-plane offset -> WGS84. Fine for the few hundred metres a demo covers."""
    dlat = math.degrees(north_m / EARTH_RADIUS_M)
    dlon = math.degrees(east_m / (EARTH_RADIUS_M * math.cos(math.radians(lat0))))
    return lat0 + dlat, lon0 + dlon


def latlon_to_offset(lat0: float, lon0: float, lat: float, lon: float) -> tuple[float, float]:
    north = math.radians(lat - lat0) * EARTH_RADIUS_M
    east = math.radians(lon - lon0) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    return north, east


def pixel_to_ground(u: float, v: float, pose: Pose, cam: CameraModel,
                    err: ErrorModel = ErrorModel()) -> GeoResult:
    if pose.altitude_agl_m is None or pose.altitude_agl_m <= 0.5:
        return GeoResult(None, None, None, METHOD, "altitude_invalid")
    if abs(pose.pitch_deg) > err.max_tilt_deg or abs(pose.roll_deg) > err.max_tilt_deg:
        return GeoResult(None, None, None, METHOD, "attitude_exceeds_limit")

    ray_cam = np.array([(u - cam.cx) / cam.fx, (v - cam.cy) / cam.fy, 1.0])
    ray_ned = _rot_body_to_ned(pose.heading_deg, pose.pitch_deg, pose.roll_deg) @ (_CAM_TO_BODY @ ray_cam)
    ray_ned /= np.linalg.norm(ray_ned)
    down = ray_ned[2]
    off_nadir = math.degrees(math.acos(max(-1.0, min(1.0, down))))
    if down <= 0 or off_nadir > err.max_off_nadir_deg:
        return GeoResult(None, None, None, METHOD, "ray_too_oblique")

    t = pose.altitude_agl_m / down
    north, east = t * ray_ned[0], t * ray_ned[1]
    lat, lon = offset_to_latlon(pose.lat, pose.lon, north, east)

    ground_dist = math.hypot(north, east)
    slant = t
    gsd = pose.altitude_agl_m / cam.fx  # metres per pixel at nadir
    sigma = math.sqrt(
        err.gnss_m ** 2
        + (err.altitude_m * ground_dist / pose.altitude_agl_m) ** 2
        + (math.radians(err.attitude_deg) * slant / max(down, 0.2)) ** 2
        + err.foot_point_m ** 2
        + (2 * gsd) ** 2
    )
    return GeoResult(lat, lon, round(sigma, 2), METHOD)


def ground_to_pixel(lat: float, lon: float, pose: Pose, cam: CameraModel) -> tuple[float, float] | None:
    """Inverse projection, used by the synthetic data generator and tests."""
    north, east = latlon_to_offset(pose.lat, pose.lon, lat, lon)
    p_ned = np.array([north, east, pose.altitude_agl_m])
    p_body = _rot_body_to_ned(pose.heading_deg, pose.pitch_deg, pose.roll_deg).T @ p_ned
    p_cam = _CAM_TO_BODY.T @ p_body
    if p_cam[2] <= 0:
        return None
    u = cam.fx * p_cam[0] / p_cam[2] + cam.cx
    v = cam.fy * p_cam[1] / p_cam[2] + cam.cy
    if not (0 <= u < cam.width and 0 <= v < cam.height):
        return None
    return float(u), float(v)
