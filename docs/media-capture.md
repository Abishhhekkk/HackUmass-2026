# Photos and video (stored only on the Jetson)

The drone saves a compressed **photo** of every person the moment they are first detected, and
compressed **video** of the flight. Both are for later review and **are never uploaded**: the
uploader only sends the files listed in the snapshot manifest, the cloud API rejects anything
else, and the edge dashboard only serves those same files. The data folder is also git-ignored.

Both are controlled by the `photos:` and `video:` blocks in `config/edge.jetson.yaml`, and work
whether you start the full pipeline (`python -m rescue_edge.main --config ...`) or the live
runner (`python edge/edge_inference.py --config ...`).

## What is saved

```
<storage_root>/<mission_id>/
    photos/
        p1_42_1794668400120.jpg       full-resolution JPEG of the frame where track 42 first appeared
        photos_index.csv              time, track key, box, confidence, drone lat/lon/heading per photo
    video/
        seg_1794668400000.mkv         H.264, one file per 60 s (MJPG .avi if OpenCV has no GStreamer)
        seg_1794668400000.frames.csv  frame_no, true GPS-clock time, camera frame index, per frame
        video_index.csv               one row per finished segment
    v000007/sightings.csv             the structured data (estimated lat/lon per person)
```

* **Photos** are compressed by saving as JPEG (`jpeg_quality`, default 90).
* **Video** is shrunk to 1280x720 at about 15 fps and encoded as H.264 (`bitrate_kbps`, default
  3000, roughly 1.3 GB per hour; measure on your Jetson). Photos keep full resolution.
* All timestamps are on the **GPS clock** (the ESP32's), the same clock as `first_seen_utc` in
  `sightings.csv`.
* Detection is never slowed down: if the disk or encoder falls behind, a photo or video frame is
  skipped, not waited for. Recording also stops by itself below `min_free_gb` of free disk, so it
  can never starve the snapshot writer. On the first test flight, check `dropped`, `failed`,
  `disk_full` and `codec` under `photos` / `video` in `/api/v1/status` (full pipeline with
  `--serve`); with `edge_inference.py`, read the log output instead (it warns when it falls back
  to MJPG or stops recording).

## Getting latitude and longitude later

```bash
python scripts/match_media.py data/edge/snapshots/<mission_id>
```

writes `media_matches.csv` (one row per photo):

| status | meaning |
|---|---|
| `estimated_sighting` | has `lat`, `lon`, `horizontal_error_m` from `sightings.csv` |
| `unlocated_sighting` | person seen, no usable position (the reason is shown) |
| `not_confirmed` | the track was too short to count as a person (likely a false alarm) |

Photos are tied to people by the track key (`pass:track_id`, as in `sightings.csv` `track_ids`),
and to video by the nearest recorded frame time. `drone_lat`/`drone_lon` is where the *drone* was,
as a fallback; the person's estimate is `lat`/`lon`. With `edge_inference.py` there is no
`sightings.csv`: match a photo's `frame_t_utc` to the `timestamp` in its detections JSONL.

Pull one exact frame out of a segment:

```bash
ffmpeg -i video/seg_1794668400000.mkv -vf "select=eq(n\,123)" -vframes 1 frame.jpg
```

## Shrinking video further after landing

The Orin Nano has no hardware video encoder, so in flight the video is a light software H.264.
On a laptop, after landing, recompress each segment (keep the originals until you have checked
the results):

```bash
for f in video/seg_*.mkv; do ffmpeg -i "$f" -c:v libx265 -crf 28 -tag:v hvc1 "${f%.mkv}.h265.mp4"; done
```

If the video `codec` is `mjpg` (OpenCV without GStreamer), flight files are much larger: use the same
command on the `.avi` files, and plan disk space accordingly.

## Before relying on it

Not yet measured on real hardware: CPU load and temperature of H.264 encoding next to the
detector, real file sizes, and disk write speed. Fly a test first and watch the numbers above.
If the Jetson struggles, lower `video.fps`, `video.width`/`height` or `bitrate_kbps`, or set
`video.enabled: false`.
