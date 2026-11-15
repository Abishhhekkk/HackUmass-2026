# Data contract (schema_version 1)

The unit of transfer is a **snapshot**: the complete current state of one mission, built on
the Jetson. It is never a delta. Newer version = replaces older.

```
<storage_root>/<mission_id>/v000007/
    manifest.json      written last; lists every other file with sha256 + byte size
    sightings.csv      one row per estimated person, plus rows for sightings with no location
    heatmap.geojson    grid cells (Polygon) + people (Point)
    telemetry.json     latest frame-aligned drone position and pose, or unavailable reason
    flight_path.csv    drone pose history, about 1 Hz, frame-aligned (gaps kept with a reason)
    report.md          deterministic text report
<storage_root>/<mission_id>/LATEST    "7"
```

## manifest.json

| Field | Meaning |
|---|---|
| `schema_version` | `1` |
| `mission_id`, `drone_id` | `[A-Za-z0-9._-]{1,80}`, except `.` and `..` (IDs are directory names). Every record carries `drone_id`, so more drones can be added later |
| `version` | integer, +1 each time the data changes. Server keeps the highest |
| `created_at` | UTC ISO-8601, when the Jetson built the snapshot |
| `content_hash` | sha256 of `flight_path.csv`, `heatmap.geojson`, `sightings.csv`, and `telemetry.json` (sorted by name, concatenated) |
| `data_window.start/end` | first/last frame time (GPS clock) |
| `origin` | lat/lon the grid is anchored to (first located detection) |
| `cell_size_m` | grid resolution |
| `position_method` | `nadir_flat_ground_v1` |
| `heat_formula` | the cell score formula, in words |
| `summary` | `estimated_people, unlocated_tracks, occupied_cells, max_cell_count, frames_processed, detections_processed, median_error_m, p90_error_m, location_failures{reason: count}` |
| `files[]` | `{name, sha256, bytes}` for every file except the manifest itself |
| `status`, `disclaimer` | always `"estimated"` and a fixed human-review notice |

## sightings.csv

`person_id, status, lat, lon, horizontal_error_m, confidence, first_seen_utc, last_seen_utc,
n_frames, n_tracks, cell_id, position_method, unlocated_reason, track_ids`

* `status = estimated_sighting`: has `lat/lon` (WGS84 decimal degrees) and `horizontal_error_m`.
* `status = unlocated_sighting`: `lat/lon` empty, `unlocated_reason` is one of
  `no_telemetry, telemetry_stale, gnss_fix_invalid, altitude_invalid, attitude_exceeds_limit,
  ray_too_oblique, camera_resolution_mismatch, heading_unknown`. (`heading_unknown`: the
  drone's log gives no way to tell where the image top points, see `rescue_edge/srt.py`.) These may be the same people as located rows.
  They are never given the drone's own position as a fallback.
* `person_id` (`P001`…) is only stable within one snapshot. `track_ids` are
  `pass:tracker_id`, which are temporary IDs inside one pass, never identities.

## heatmap.geojson

FeatureCollection, coordinates in **[longitude, latitude]** order (GeoJSON standard).
* `properties.kind = "cell"`: Polygon; `cell_id` (`r{row}c{col}` from the origin, rows north,
  cols east), `count`, `score`, `person_ids`.
* `properties.kind = "person"`: Point; `person_id, confidence, horizontal_error_m,
  first_seen_utc, last_seen_utc, n_tracks, cell_id, status`.
* Top-level `properties`: `cell_size_m`, `origin`, `heat_formula`.

## telemetry.json

The artifact records the drone pose looked up at the latest processed frame timestamp. It is
not a detected person's location. A valid record has `available: true`, an ISO-8601
`observed_at`, a `position` object with WGS84 `lat` and `lon`, and the available pose fields
(`altitude_agl_m`, `heading_deg`, `pitch_deg`, `roll_deg`). If telemetry is missing or invalid,
`available` is false and `reason` explains why; no position is invented. The artifact is
included in each snapshot's file checksums and content hash.

## flight_path.csv

The drone's own track, for the cloud's `drone_telemetry` time-series table (a Timescale
hypertable on Tiger Data) and the dashboard's "Drone path" line. **These are drone
positions, never sighting locations.**

`observed_at, t, lat, lon, alt_agl_m, heading_deg, pitch_deg, roll_deg, valid, reason`

* One row at most every `flight_path_interval_s` (edge config, default 1.0) of flight time,
  looked up from telemetry at that frame's GPS-clock time, like detections are.
* `t` is UTC epoch seconds; `observed_at` the same instant as ISO-8601.
* `valid = 1`: pose fields are set. `valid = 0`: no usable fix at that time; position fields
  are empty and `reason` is one of the telemetry reasons above (`no_telemetry`,
  `telemetry_stale`, `gnss_fix_invalid`, `altitude_invalid`, `heading_unknown`). Gaps are
  kept, never filled.
* The file holds the whole track so far (a snapshot is full state). The cloud inserts only
  rows newer than what it already stored for that mission and drone, so re-sends are harmless.
* Optional for the server: snapshots without it (older edges) are still accepted.

## Units and frames

* Distances in metres, angles in degrees, times UTC epoch seconds internally and ISO-8601 in files.
* `alt_agl` = height above the ground under the drone (ground assumed flat).
* Heading clockwise from **true** north (the ESP32 applies magnetic declination).
  Pitch: nose up = positive. Roll: right side down = positive.
* Camera mounted pointing straight down, **top of the image toward the drone's nose**.
  If you mount it differently, change `_CAM_TO_BODY` in `geolocate.py`.

## Upload protocol

`POST /api/v1/snapshots` (multipart: `manifest` + `files`), header `X-API-Key`.

| Response | Meaning | Jetson does |
|---|---|---|
| `201 {"status":"accepted"}` | stored | marks version acknowledged |
| `200 {"status":"duplicate"}` | already had this exact version | marks acknowledged |
| `200 {"status":"stale"}` | server already has a newer version | marks acknowledged |
| `422` checksum/file mismatch | transfer was cut or corrupted; nothing stored | retries with backoff |
| `409` same version, different content | edge storage was reset mid-mission | needs a human; start a new mission ID |
| `401`, `413` | bad key / too big (default 10 MB) | fix config |

## ESP32 → Jetson telemetry line

```json
{"t":1794668400.120,"lat":42.3890100,"lon":-72.5283000,"alt_agl":24.0,"hdg":91.0,
 "pitch":0.8,"roll":-1.2,"fix":3,"sats":11,"hdop":0.9}
```
`t` is GPS UTC time. A sample counts as valid if `fix >= 3`, `hdop <= 5` and `alt_agl > 0`.
