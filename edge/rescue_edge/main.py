"""Edge pipeline: frames -> detections/tracks -> geolocation -> aggregate -> snapshots.

    python -m rescue_edge.main --config config/edge.mock.yaml --serve

Everything here works with no network. The uploader (if enabled) runs in its own thread.
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import yaml

from .aggregate import Aggregator
from .capture import CameraSource, ReplaySource, VideoFileSource, csi_pipeline
from .detector import build_detector
from .geolocate import CameraModel, ErrorModel, pixel_to_ground
from .report import iso
from .scheduler import AdaptiveScheduler, SchedulerConfig
from .snapshot import SnapshotWriter
from .telemetry import FlightPathRecorder, SerialTelemetryReader, TelemetryBuffer, load_csv
from .uploader import Uploader

log = logging.getLogger("rescue_edge")


def _parse_time(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()


class Pipeline:
    def __init__(self, cfg: dict, base_dir: Path):
        self.cfg, self.base = cfg, base_dir
        self.mission_id = cfg.get("mission_id") or f"m-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        self.drone_id = cfg.get("drone_id", "drone-01")
        self.root = self._path(cfg.get("storage_root", "data/edge/snapshots"))

        tcfg = cfg["telemetry"]
        # A replayed log is loaded whole before the first frame, so it must not be capped;
        # a live serial feed keeps a bounded rolling window.
        self.telemetry = TelemetryBuffer(tcfg.get("max_gap_s", 1.0), tcfg.get("max_age_s", 2.0),
                                         max_samples=None if tcfg["kind"] == "replay" else 512)
        self.serial_reader = None
        if tcfg["kind"] == "replay":
            load_csv(self._path(tcfg["path"]), self.telemetry)
        elif tcfg["kind"] == "serial":
            self.serial_reader = SerialTelemetryReader(
                tcfg["port"], tcfg.get("baud", 115200), self.telemetry,
                str(self._path(tcfg["log_path"])) if tcfg.get("log_path") else None)
        else:
            raise ValueError(f"unknown telemetry kind {tcfg['kind']!r}")

        self.camera = CameraModel.from_config(cfg["camera"])
        self.error_model = ErrorModel(**cfg.get("error_model", {}))
        self.detector = build_detector(cfg["detector"])
        a = cfg.get("aggregate", {})
        self.agg = Aggregator(min_frames=a.get("min_frames", 3), merge_radius_m=a.get("merge_radius_m", 4.0),
                              cell_size_m=a.get("cell_size_m", 10.0),
                              recency_tau_s=a.get("recency_tau_s", 1800.0))
        self.scheduler = AdaptiveScheduler(SchedulerConfig(**cfg.get("scheduler", {})))
        self.writer = SnapshotWriter(self.root, self.mission_id, self.drone_id)
        self.snapshot_interval_s = cfg.get("snapshot_interval_s", 10.0)
        # Drone track for flight_path.csv -> drone_telemetry (Timescale hypertable on Tiger Data).
        self.flight_path = FlightPathRecorder(cfg.get("flight_path_interval_s", 1.0))

        ucfg = cfg.get("uploader", {})
        self.uploader = None
        if ucfg.get("enabled"):
            self.uploader = Uploader(
                self.root, self.mission_id, ucfg["server_url"],
                api_key=os.environ.get(ucfg.get("api_key_env", "INGEST_API_KEY")),
                offline_flag_file=str(self._path(ucfg["offline_flag_file"])) if ucfg.get("offline_flag_file") else None,
                base_backoff_s=ucfg.get("base_backoff_s", 2.0), max_backoff_s=ucfg.get("max_backoff_s", 60.0))
        self.running = False
        self.finished = False
        self.last_frame_t: float | None = None
        self._warned_resolution = False

    def _path(self, p: str) -> Path:
        p = Path(p)
        return p if p.is_absolute() else self.base / p

    def _source(self):
        s = self.cfg["source"]
        if s["kind"] == "replay":
            return ReplaySource(self._path(s["path"]), s.get("realtime", False), s.get("speed", 1.0))
        if s["kind"] == "video":
            return VideoFileSource(str(self._path(s["path"])), _parse_time(s["video_start_time"]),
                                   s.get("realtime", False))
        if s["kind"] == "camera":
            dev = s.get("device", 0)
            if dev == "csi":
                dev = csi_pipeline(s.get("width", 1920), s.get("height", 1080), s.get("fps", 30))
            clock = self.serial_reader.now if self.serial_reader else time.time
            return CameraSource(dev, clock)
        raise ValueError(f"unknown source kind {s['kind']!r}")

    def _resolution_mismatch(self, frame) -> bool:
        """True if the frame size differs from the calibrated camera.width/height."""
        if not frame.width or not frame.height:
            return False  # source did not report a size
        if (frame.width, frame.height) == (self.camera.width, self.camera.height):
            return False
        if not self._warned_resolution:
            log.warning("frame is %dx%d but camera config is %dx%d: detections will be kept "
                        "unlocated (camera_resolution_mismatch). Fix the camera: block.",
                        frame.width, frame.height, self.camera.width, self.camera.height)
            self._warned_resolution = True
        return True

    def snapshot(self, force: bool = False) -> int | None:
        state = self.agg.finalize()
        telemetry = None
        if self.last_frame_t is not None:
            lookup = self.telemetry.at(self.last_frame_t)
            telemetry = {
                "schema_version": 1,
                "available": lookup.pose is not None,
                "observed_at": iso(self.last_frame_t),
                "source": "edge_telemetry",
                "alignment": "TelemetryBuffer lookup at the latest processed frame timestamp",
            }
            if lookup.pose is None:
                telemetry["reason"] = lookup.reason
            else:
                pose = lookup.pose
                telemetry.update({
                    "position": {"lat": pose.lat, "lon": pose.lon},
                    "altitude_agl_m": pose.altitude_agl_m,
                    "heading_deg": pose.heading_deg,
                    "pitch_deg": pose.pitch_deg,
                    "roll_deg": pose.roll_deg,
                })
        v = self.writer.write_if_changed(state, force=force, telemetry=telemetry,
                                         flight_path=self.flight_path.rows())
        if v:
            log.info("snapshot v%d written (%d people)", v, len(state.people))
            if self.uploader:
                self.uploader.poke()
        return v

    def run(self) -> None:
        self.running = True
        if self.serial_reader:
            self.serial_reader.start()
        if self.uploader:
            self.uploader.start()
        last_snap_t = None
        try:
            for frame in self._source():
                self.agg.note_frame(frame.t)
                self.last_frame_t = frame.t
                if self.flight_path.due(frame.t):
                    self.flight_path.record(frame.t, self.telemetry.at(frame.t))
                if self.scheduler.should_process(frame.index):
                    dets = self.detector(frame)
                    look = self.telemetry.at(frame.t)
                    mismatch = self._resolution_mismatch(frame)
                    for d in dets:
                        if mismatch:
                            # Projecting with the wrong intrinsics would give confident but
                            # wrong coordinates; keep the sighting unlocated instead.
                            self.agg.add(frame.t, d.track_id, d.confidence, None, None, None,
                                         "camera_resolution_mismatch")
                            continue
                        if look.pose is None:
                            self.agg.add(frame.t, d.track_id, d.confidence, None, None, None, look.reason)
                            continue
                        g = pixel_to_ground(*d.foot_pixel, look.pose, self.camera, self.error_model)
                        self.agg.add(frame.t, d.track_id, d.confidence, g.lat, g.lon,
                                     g.horizontal_error_m, g.reason)
                    self.scheduler.update(frame.t, len(dets))
                if last_snap_t is None:
                    last_snap_t = frame.t
                elif frame.t - last_snap_t >= self.snapshot_interval_s:
                    self.snapshot()
                    last_snap_t = frame.t
            self.snapshot(force=True)  # final snapshot always carries the full frame counts
        finally:
            self.running, self.finished = False, True
            if self.serial_reader and self.serial_reader.is_alive():
                self.serial_reader.stop()
                self.serial_reader.join(timeout=2.0)
                if self.serial_reader.is_alive():
                    log.error("telemetry reader did not stop within 2 seconds")
            log.info("pipeline finished: %s", self.scheduler.status())

    def status(self) -> dict:
        return {"role": "edge", "mission_id": self.mission_id, "drone_id": self.drone_id,
                "running": self.running, "finished": self.finished,
                "frames": self.agg.frames, "detections": self.agg.detections,
                "scheduler": self.scheduler.status(),
                "demo_controls_available": bool(self.uploader),
                "network_demo_available": bool(self.uploader and self.uploader.offline_flag),
                "uploader": self.uploader.status() if self.uploader else None,
                "telemetry_samples": len(self.telemetry)}


def load_config(path: str) -> tuple[dict, Path]:
    p = Path(path).resolve()
    cfg = yaml.safe_load(p.read_text())
    # relative paths in the config are relative to the repo root (config/..)
    return cfg, p.parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(description="RescueSight edge pipeline")
    ap.add_argument("--config", required=True)
    ap.add_argument("--serve", action="store_true", help="also serve the local dashboard")
    ap.add_argument("--mission-id")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg, base = load_config(args.config)
    if args.mission_id:
        cfg["mission_id"] = args.mission_id
    pipe = Pipeline(cfg, base)
    log.info("mission %s, snapshots in %s", pipe.mission_id, pipe.root)
    if not args.serve:
        pipe.run()
        if pipe.uploader:  # give the uploader a moment to send the final snapshot
            for _ in range(20):
                if not pipe.uploader.status()["pending"]:
                    break
                time.sleep(0.5)
        return
    import uvicorn

    from .local_server import create_app

    threading.Thread(target=pipe.run, daemon=True).start()
    ls = cfg.get("local_server", {})
    uvicorn.run(create_app(pipe, base / "web"), host=ls.get("host", "0.0.0.0"),
                port=ls.get("port", 8080), log_level="warning")


if __name__ == "__main__":
    main()
