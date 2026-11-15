"""First-sighting photos: one compressed JPEG per new track, plus a timestamp index.

When the detector reports a track_id the pipeline has not photographed yet, the frame that
produced it is saved as a JPEG. Saving as JPEG is the compression step (quality ~90 is a
few hundred KB for a 1080p frame instead of ~6 MB raw).

    <root>/<mission_id>/photos/
        p1_42_1794668400120.jpg    <pass>_<track>_<frame time in epoch ms>
        photos_index.csv           one row per photo, joined to sightings.csv later

Matching to the structured data:
  * track_key  == the `pass:track_id` value in sightings.csv `track_ids`
  * frame_t_utc is the frame's GPS-clock time, the same clock as first_seen_utc
  * drone_* columns hold the drone pose at that frame, as a fallback when a track id is lost

Encoding and disk writes happen on a background thread behind a small bounded queue, so a
slow disk never stalls detection; if the queue is full the photo is skipped and the track is
offered again on its next detection. The index is flushed to disk after every row, and photos
are written to a temp name and renamed, so a power cut never leaves a half-written JPEG.
"""
from __future__ import annotations

import csv
import logging
import os
import queue
import re
import threading
from pathlib import Path

from .report import iso

log = logging.getLogger("rescue_edge.photos")

INDEX_FIELDS = [
    "file", "track_key", "frame_t_utc", "frame_t_epoch", "frame_index", "confidence",
    "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2", "image_width", "image_height", "jpeg_bytes",
    "drone_lat", "drone_lon", "drone_altitude_agl_m", "drone_heading_deg", "drone_pitch_deg",
    "drone_roll_deg", "drone_pose_unavailable_reason",
]


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


class PhotoWriter(threading.Thread):
    def __init__(self, root: str | Path, mission_id: str, quality: int = 90,
                 queue_size: int = 8, max_photos: int = 500):
        super().__init__(daemon=True)
        self.dir = Path(root) / mission_id / "photos"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "photos_index.csv"
        self.quality, self.max_photos = int(quality), int(max_photos)
        self.q: queue.Queue = queue.Queue(maxsize=queue_size)
        self.known: set[str] = self._load_known()   # tracks already photographed (survives restarts)
        self.saved = self.dropped = self.failed = 0
        self._halt = threading.Event()
        self._cap_logged = False

    # ------------------------------------------------------------------ producer side
    def submit(self, image, t: float, frame_index: int, track_key: str, confidence: float,
               bbox, pose=None, pose_reason: str | None = None) -> bool:
        """Queue a photo of this track's first frame. Never blocks. True if accepted.

        `image` is kept by reference (not copied); the pipeline never modifies a frame in place.
        """
        if track_key in self.known:
            return False
        if len(self.known) >= self.max_photos:
            if not self._cap_logged:
                log.warning("photo limit of %d reached; no more first-sighting photos", self.max_photos)
                self._cap_logged = True
            return False
        try:
            self.q.put_nowait((image, t, frame_index, track_key, confidence, bbox, pose, pose_reason))
        except queue.Full:
            self.dropped += 1
            return False
        self.known.add(track_key)
        return True

    def status(self) -> dict:
        return {"tracks_photographed": len(self.known), "saved": self.saved, "queued": self.q.qsize(),
                "dropped": self.dropped, "failed": self.failed, "max_photos": self.max_photos}

    def stop(self, timeout: float = 5.0) -> None:
        """Flush whatever is queued, then stop."""
        self._halt.set()
        if self.is_alive():
            self.join(timeout)

    # ------------------------------------------------------------------ writer thread
    def run(self) -> None:
        while True:
            try:
                item = self.q.get(timeout=0.2)
            except queue.Empty:
                if self._halt.is_set():
                    break
                continue
            try:
                self._save(*item)
                self.saved += 1
            except Exception:
                self.failed += 1
                log.exception("could not save photo for %s", item[3])

    def _save(self, image, t, frame_index, track_key, confidence, bbox, pose, pose_reason) -> None:
        import cv2

        ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            raise RuntimeError("JPEG encoding failed")
        data = buf.tobytes()
        name = f"{_safe(track_key)}_{int(round(t * 1000))}.jpg"
        tmp = self.dir / (name + ".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.dir / name)
        h, w = image.shape[:2]
        x1, y1, x2, y2 = (round(float(v), 1) for v in bbox)
        row = {
            "file": name, "track_key": track_key, "frame_t_utc": iso(t), "frame_t_epoch": f"{t:.3f}",
            "frame_index": frame_index, "confidence": round(float(confidence), 3),
            "bbox_x1": x1, "bbox_y1": y1, "bbox_x2": x2, "bbox_y2": y2,
            "image_width": w, "image_height": h, "jpeg_bytes": len(data),
            "drone_lat": "", "drone_lon": "", "drone_altitude_agl_m": "", "drone_heading_deg": "",
            "drone_pitch_deg": "", "drone_roll_deg": "", "drone_pose_unavailable_reason": "",
        }
        if pose is not None:
            row.update({"drone_lat": f"{pose.lat:.7f}", "drone_lon": f"{pose.lon:.7f}",
                        "drone_altitude_agl_m": pose.altitude_agl_m, "drone_heading_deg": pose.heading_deg,
                        "drone_pitch_deg": pose.pitch_deg, "drone_roll_deg": pose.roll_deg})
        else:
            row["drone_pose_unavailable_reason"] = pose_reason or "no_telemetry"
        self._append_row(row)

    def _append_row(self, row: dict) -> None:
        new = not self.index_path.exists() or self.index_path.stat().st_size == 0
        with open(self.index_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, lineterminator="\n")
            if new:
                w.writeheader()
            w.writerow(row)
            f.flush()
            os.fsync(f.fileno())

    def _load_known(self) -> set[str]:
        if not self.index_path.exists():
            return set()
        with open(self.index_path, newline="") as f:
            return {r["track_key"] for r in csv.DictReader(f) if r.get("track_key")}
