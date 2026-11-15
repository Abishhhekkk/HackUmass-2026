# Jetson Orin Nano live inference

> **This page covers `edge/edge_inference.py`, a standalone detection logger for bench
> testing the TensorRT engine, camera and ESP32 serial link together.** It writes one JSON
> line per detection and nothing else: no de-duplication, heat map, snapshots, upload or
> dashboard. **For the demo, run the mission pipeline instead**, which feeds the map and the
> cloud: `cd edge && python -m rescue_edge.main --config ../config/<config>.yaml --serve`.
> Sections 1 and 2 (Jetson setup, engine export) apply to both.

This workflow uses the edge pipeline's existing frame-aligned telemetry and nadir-camera
projection. It reports **estimated person locations**; it is not a survey-grade coordinate
system and must not be used as an autonomous rescue or flight-control command.

## 1. Prepare the Jetson

TensorRT engines are device- and software-stack-specific. Build and run the engine on the
target Orin Nano after installing the JetPack 6-compatible PyTorch, TensorRT, and Ultralytics
versions. Follow the bring-up guidance in [hardware.md](./hardware.md) rather than installing
desktop PyTorch wheels.

Install the project's Python dependencies, using the system OpenCV build with GStreamer on
Jetson:

```bash
python3 -m pip install -r edge/requirements.txt
```

If installing `edge/requirements.txt` would replace the Jetson system OpenCV, install
`pyserial`, `numpy`, and the other Python requirements separately and keep the system OpenCV.
The JetPack-provided CUDA, PyTorch, TensorRT, and Ultralytics versions must be compatible.

## 2. Export a fixed-size FP16 TensorRT engine

From the repository root, on the Jetson:

```bash
python3 edge/export_model.py \
  --model yolov8n.pt \
  --output models/yolov8n.engine \
  --imgsz 640
```

Ultralytics downloads `yolov8n.pt` if it is not already cached. You can instead pass
`yolo11n.pt`. The exporter requests FP16, batch size 1, a fixed 640 x 640 input, and a
2 GiB TensorRT workspace. The script refuses to build on non-ARM64 hosts. Use the same
image size when running inference.

For small, distant people, 640 may miss detections. Validate against representative flight
footage; if you need a larger size, export with the new size and pass the same `--imgsz`
when running the live script. Benchmark temperature, FPS, and detection recall on the
actual Jetson and camera before flight.

## 3. Configure and run

Review `config/edge.jetson.yaml` before connecting hardware:

* `source` must match the camera and its actual capture dimensions.
* `camera.width`, `camera.height`, and calibrated `fx`, `fy`, `cx`, `cy` must describe that
  same image. `hfov_deg` is only a rough fallback; calibrate the lens when possible.
* `detector.model_path` must point to the exported engine and `detector.imgsz` must match
  its build size.
* `telemetry.port` is `/dev/ttyUSB0` by default; some ESP32 boards enumerate as
  `/dev/ttyACM0`. Give the user running the process permission to access that device.

Then run from the repository root:

```bash
python3 edge/edge_inference.py --config config/edge.jetson.yaml \
  --output data/edge/detections.jsonl
```

Use `--output -` to send one JSON object per detection to stdout. Status messages go to
stderr. `--serial-port`, `--baud`, `--model`, `--imgsz`, and `--confidence` override config
values. Add `--projection-point center` to project the bounding-box center; the default
`foot` uses its bottom-center, a better ground-contact estimate for person detections.
Press Ctrl+C to stop; the camera and serial thread are closed.

An unlocated detection is still logged, with `target_lat` and `target_lon` set to `null` and
`unlocated_reason` describing missing, stale, or invalid telemetry, or a mismatch between
the camera resolution and calibration. Do not substitute the drone's own coordinates.

Example JSONL record:

```json
{"object_class":"person","confidence":0.84,"target_lat":42.389012,"target_lon":-72.528291,"timestamp":"2026-11-14T18:40:00.120+00:00","horizontal_error_m":4.2,"position_method":"nadir_flat_ground_v1","unlocated_reason":null}
```

## 4. ESP32 serial line format

The ESP32 writes one UTF-8 JSON object followed by `\n` for each sample, at 115200 baud.
The Jetson buffers samples off the camera thread and interpolates pose to the frame's GPS
time. Field names intentionally match `firmware/esp32_telemetry/esp32_telemetry.ino` and
the [snapshot data contract](./data-contract.md).

```json
{"t":1794668400.120,"lat":42.3890100,"lon":-72.5283000,"alt_agl":24.0,"hdg":91.0,"pitch":0.8,"roll":-1.2,"fix":3,"sats":11,"hdop":0.9}
```

| Field | Type / units | Meaning |
|---|---|---|
| `t` | number, UTC epoch seconds | GPS time used to align the pose to a camera frame |
| `lat`, `lon` | numbers, decimal degrees | WGS84 antenna position |
| `alt_agl` | number, metres | Height above local ground; `<= 0` is unusable |
| `hdg` | number, degrees | Yaw/heading clockwise from true north, range `[0, 360)` |
| `pitch` | number, degrees | Nose-up is positive |
| `roll` | number, degrees | Right side down is positive |
| `fix` | integer | `3` means a 3D GNSS fix; lower values are rejected |
| `sats` | integer | Satellites used/reported by the receiver |
| `hdop` | number | Horizontal dilution of precision; values above `5` are rejected |

Coordinates must be valid WGS84 values; pose and time must be finite numbers. Samples without
a 3D fix, with HDOP above 5, invalid AGL altitude, or stale frame alignment do not produce a
target coordinate. `sats` is informative; validity is determined from `fix` and `hdop`.

For a serial-terminal smoke test, paste this **single line** repeatedly with a current UTC
epoch value and a realistic positive AGL altitude:

```text
{"t":1794668400.120,"lat":42.3890100,"lon":-72.5283000,"alt_agl":24.0,"hdg":91.0,"pitch":0.8,"roll":-1.2,"fix":3,"sats":11,"hdop":0.9}
```

The `t` value must stay within the configured telemetry age of the image frame. Reusing an
old example timestamp intentionally results in unlocated detections.

## Projection limits

`nadir_flat_ground_v1` casts the calibrated camera ray through the selected pixel into a
flat ground plane at `alt_agl`, then rotates it by the ESP32 pitch/roll/true heading and
converts the local north/east offset to WGS84. It assumes a downward-facing camera, known
camera-to-airframe alignment, negligible/removed lens distortion, known AGL altitude, and
locally flat terrain. GPS altitude referenced to a takeoff fix can be off by several metres;
a downward rangefinder and calibrated camera/IMU mounting materially improve the estimate.
Terrain relief, wind/vehicle motion, timing error, GNSS error, and detector box placement
also affect the result. Inspect the reported `horizontal_error_m`, validate with measured
ground-truth points, and require human review.
