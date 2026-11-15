"""Compressed flight video, stored ONLY on the Jetson (never uploaded).

Every camera frame is offered to the recorder; it keeps about `fps` of them, shrinks them to
`width` x `height` and writes H.264 video in short segments:

    <root>/<mission_id>/video/
        seg_1794668400120.mkv             one file per `segment_s` seconds; name = GPS start time (ms)
        seg_1794668400120.frames.csv      frame_no, frame_t_epoch, source_frame_index
        video_index.csv                   one row per finished segment

Matching a time to the video: find the segment whose start <= t <= end, then look up the
nearest row in its .frames.csv. TRUST frames.csv OVER THE VIDEO'S OWN CLOCK: the file is
written at a nominal fps, but the loop may skip frames when it is busy, so playback speed can
differ slightly from real time. frames.csv always holds each frame's true GPS-clock time.

Codec: H.264 through GStreamer's x264enc (software; the Orin Nano has no hardware video
encoder, so threads are capped to protect the detection model). If OpenCV was built without
GStreamer, it falls back to MJPG, which works everywhere but makes MUCH larger files; shrink
those after landing with ffmpeg (see docs/media-capture.md).

Safety, same ideas as photos.py: encoding runs on its own thread behind a bounded queue and
frames are skipped (never waited for) if it falls behind, so detection is never slowed.
Short segments mean a power cut loses at most the last few seconds of the current file.
Recording stops by itself when free disk space falls below `min_free_gb`, so video can never
starve the snapshot writer of space.
"""
from __future__ import annotations

import csv
import logging
import os
import queue
import re
import shutil
import threading
from pathlib import Path

from .report import iso

log = logging.getLogger("rescue_edge.video")

INDEX_FIELDS = ["file", "start_t_epoch", "start_utc", "end_t_epoch", "end_utc", "frames",
                "width", "height", "fps", "codec", "bytes"]
FRAME_FIELDS = ["frame_no", "frame_t_epoch", "source_frame_index"]


def _opencv_has_gstreamer() -> bool:
    import cv2

    return re.search(r"GStreamer:\s+YES", cv2.getBuildInformation()) is not None


class _Segment:
    def __init__(self, base: Path, start: float, writer, codec: str, path: Path, w: int, h: int):
        self.base, self.start, self.writer, self.codec, self.path = base, start, writer, codec, path
        self.width, self.height = w, h
        self.last_t = start
        self.n = 0
        self.frames_file = open(base.with_suffix(".frames.csv"), "w", newline="")
        self.frames_csv = csv.writer(self.frames_file, lineterminator="\n")
        self.frames_csv.writerow(FRAME_FIELDS)


class VideoRecorder(threading.Thread):
    def __init__(self, root: str | Path, mission_id: str, width: int = 1280, height: int = 720,
                 fps: float = 15.0, segment_s: float = 60.0, codec: str = "auto",
                 bitrate_kbps: int = 3000, threads: int = 2, queue_size: int = 15,
                 min_free_gb: float = 2.0):
        super().__init__(daemon=True)
        self.dir = Path(root) / mission_id / "video"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "video_index.csv"
        self.width, self.height, self.fps = int(width), int(height), float(fps)
        self.segment_s, self.codec = float(segment_s), codec
        self.bitrate_kbps, self.threads = int(bitrate_kbps), int(threads)
        self.min_free_bytes = int(float(min_free_gb) * 1024 ** 3)
        self.q: queue.Queue = queue.Queue(maxsize=queue_size)
        self._min_gap = 0.9 / self.fps          # keep ~fps frames per second out of what arrives
        self._last_t: float | None = None
        self.accepted = self.dropped = self.written = self.failed = self.segments = 0
        self.disk_full = False
        self.disabled = False                   # too many failures in a row: give up, keep flying
        self._consecutive_failures = 0
        self.codec_used: str | None = None
        self._halt = threading.Event()
        self._warned_mjpg = False

    @classmethod
    def from_config(cls, cfg: dict, root, mission_id: str) -> "VideoRecorder":
        return cls(root, mission_id, width=cfg.get("width", 1280), height=cfg.get("height", 720),
                   fps=cfg.get("fps", 15), segment_s=cfg.get("segment_s", 60),
                   codec=cfg.get("codec", "auto"), bitrate_kbps=cfg.get("bitrate_kbps", 3000),
                   threads=cfg.get("threads", 2), queue_size=cfg.get("queue_size", 15),
                   min_free_gb=cfg.get("min_free_gb", 2.0))

    # ------------------------------------------------------------------ producer side
    def submit(self, image, t: float, frame_index: int) -> bool:
        """Offer a camera frame. Never blocks. True if it was queued for recording.

        `image` is kept by reference until the writer thread has resized it; the pipeline never
        modifies a frame in place.
        """
        if self.disk_full or self.disabled:
            return False
        if self._last_t is not None and 0 <= t - self._last_t < self._min_gap:
            return False                          # frame-rate decimation, not a drop
        try:
            self.q.put_nowait((image, t, frame_index))
        except queue.Full:
            self.dropped += 1
            return False
        self._last_t = t
        self.accepted += 1
        return True

    def status(self) -> dict:
        return {"codec": self.codec_used, "frames_written": self.written, "segments": self.segments,
                "queued": self.q.qsize(), "dropped": self.dropped, "failed": self.failed,
                "disk_full": self.disk_full}

    def stop(self, timeout: float = 10.0) -> None:
        """Write what is queued, close the current segment, then stop."""
        self._halt.set()
        if self.is_alive():
            self.join(timeout)

    # ------------------------------------------------------------------ writer thread
    def run(self) -> None:
        seg = None
        while True:
            try:
                image, t, idx = self.q.get(timeout=0.2)
            except queue.Empty:
                if self._halt.is_set():
                    break
                continue
            if self.disabled or self.disk_full:
                continue                          # recording has given up; just drain the queue
            try:
                seg = self._write(seg, image, t, idx)
                self._consecutive_failures = 0
            except Exception:
                self.failed += 1
                self._consecutive_failures += 1
                seg = self._abandon(seg)
                if self._consecutive_failures >= 5:
                    self.disabled = True
                    log.exception("video recording failed 5 times in a row; recording disabled "
                                  "(detection and snapshots are unaffected)")
                else:
                    log.exception("video write failed; starting a new segment")
        self._close(seg)

    def _write(self, seg, image, t: float, idx: int):
        import cv2

        if seg is not None and not (0 <= t - seg.start < self.segment_s):
            seg = self._close(seg)                # time is up (or the clock jumped back)
        if seg is None:
            if shutil.disk_usage(self.dir).free < self.min_free_bytes:
                self.disk_full = True
                log.error("free disk space below %.1f GB; video recording stopped",
                          self.min_free_bytes / 1024 ** 3)
                return None
            seg = self._open(t)
        if image.shape[1] != self.width or image.shape[0] != self.height:
            image = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_AREA)
        seg.writer.write(image)
        seg.frames_csv.writerow([seg.n, f"{t:.3f}", idx])
        seg.n += 1
        seg.last_t = t
        self.written += 1
        if seg.n % 30 == 0:
            seg.frames_file.flush()
        return seg

    def _open(self, t: float) -> _Segment:
        base = self.dir / f"seg_{int(round(t * 1000))}"
        writer, codec, path = self._make_writer(base)
        self.codec_used = codec
        self.segments += 1
        return _Segment(base, t, writer, codec, path, self.width, self.height)

    def _make_writer(self, base: Path):
        import cv2

        if self.codec in ("auto", "h264") and _opencv_has_gstreamer():
            path = base.with_suffix(".mkv")
            pipeline = (
                "appsrc ! videoconvert ! "
                f"x264enc speed-preset=ultrafast tune=zerolatency bitrate={self.bitrate_kbps} "
                f"threads={self.threads} key-int-max={max(1, int(self.fps * 2))} ! "
                f'h264parse ! matroskamux ! filesink location="{path}"'
            )
            try:
                writer = cv2.VideoWriter(pipeline, cv2.CAP_GSTREAMER, 0, self.fps,
                                         (self.width, self.height), True)
                if writer.isOpened():
                    return writer, "h264", path
                writer.release()
            except cv2.error as e:  # this build lacks the backend, or the pipeline is invalid
                log.warning("GStreamer H.264 pipeline could not start: %s", e)
        if self.codec == "h264" and not self._warned_mjpg:
            log.warning("H.264 writer unavailable on this OpenCV build; using MJPG instead")
        if not self._warned_mjpg:
            log.warning("recording MJPG video: files will be MUCH larger than H.264; "
                        "recompress after landing (see docs/media-capture.md)")
            self._warned_mjpg = True
        path = base.with_suffix(".avi")
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), self.fps,
                                 (self.width, self.height))
        if not writer.isOpened():
            raise RuntimeError("no usable video writer (neither H.264 nor MJPG)")
        return writer, "mjpg", path

    def _close(self, seg):
        if seg is None:
            return None
        try:
            seg.writer.release()
        finally:
            seg.frames_file.flush()
            os.fsync(seg.frames_file.fileno())
            seg.frames_file.close()
        size = seg.path.stat().st_size if seg.path.exists() else 0
        self._append_index({
            "file": seg.path.name, "start_t_epoch": f"{seg.start:.3f}", "start_utc": iso(seg.start),
            "end_t_epoch": f"{seg.last_t:.3f}", "end_utc": iso(seg.last_t), "frames": seg.n,
            "width": seg.width, "height": seg.height, "fps": self.fps, "codec": seg.codec, "bytes": size,
        })
        return None

    def _abandon(self, seg):
        """After a failed write: close what we can and let the next frame open a fresh segment."""
        try:
            return self._close(seg)
        except Exception:
            log.exception("could not cleanly close the broken segment")
            return None

    def _append_index(self, row: dict) -> None:
        new = not self.index_path.exists() or self.index_path.stat().st_size == 0
        with open(self.index_path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_FIELDS, lineterminator="\n")
            if new:
                w.writeheader()
            w.writerow(row)
            f.flush()
            os.fsync(f.fileno())
