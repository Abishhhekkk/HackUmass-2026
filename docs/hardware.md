# Hardware bring-up: Jetson Orin Nano + camera + ESP32 telemetry

Work through these in order. Each step has a check, so you know it works before you stack the next thing on it.

## 1. Jetson Orin Nano software

1. Record what you have: `cat /etc/nv_tegra_release`, `dpkg -l | grep nvidia-jetpack`,
   `free -h`, camera model and lens. Put them in the team notes.
2. Power mode and clocks for benchmarking: `sudo nvpmodel -q`, then `sudo jetson_clocks`.
   Watch with `tegrastats` or `jtop`.
3. Install Ultralytics following the **Ultralytics NVIDIA Jetson guide** for your JetPack
   version. It lists the Jetson-specific PyTorch/torchvision wheels. Don't use the desktop
   `pip install torch`, which gives you a CPU-only build.
4. Repo dependencies: `pip install -r edge/requirements.txt pyserial`.
5. **Check:** `yolo predict model=yolov8n.pt source=<a test clip> classes=0` produces boxes.
6. TensorRT, built **on the Jetson**, since engines are tied to the device:
   `yolo export model=yolov8n.pt format=engine half=True imgsz=960`
   then copy `yolov8n.engine` to `models/` and set `detector.model_path` in
   `config/edge.jetson.yaml`. Keep `imgsz` the same in export and config.
7. **Check:** compare FPS and the boxes on the same clip between `.pt` and `.engine`. Write down
   the FPS, temperature and power mode. Those are your demo numbers.

Aerial footage: COCO-trained YOLO struggles with small people seen from above. Before
fine-tuning, try a higher `imgsz`, flying lower, and a lower `conf_threshold` on your own
clip. Fine-tune on VisDrone or HERIDAL only if that still misses people.

## 2. Camera

* CSI camera (e.g. IMX219): `source.device: csi`. Test first with
  `gst-launch-1.0 nvarguscamerasrc ! nvvidconv ! xvimagesink`.
* USB webcam: `source.device: 0`.
* Mount pointing **straight down** with the **top of the image toward the nose**.
* Calibration (recommended): print an OpenCV checkerboard and run a standard
  `cv2.calibrateCamera` script. Put `fx, fy, cx, cy` into `camera:` in the config. Without it,
  `hfov_deg` is a rough stand-in.

## 3. ESP32 telemetry (`firmware/esp32_telemetry`)

The ESP32 has **no GPS of its own**. Parts: NEO-M8N GPS (often sold with a QMC5883L compass
on the same puck), optional MPU6050 for tilt, optional barometer or TF-Luna rangefinder for
altitude.

Bench checklist. Open the serial monitor at 115200 baud, or `cat /dev/ttyUSB0` on the Jetson:
1. Outdoors: `fix` becomes 3 and `sats` ≥ 6 within a few minutes. `t` matches real UTC.
2. Point the nose north, then east: `hdg` ≈ 0, then ≈ 90. If not, fix the axis/offset in
   `headingDeg()`. Keep the compass away from motors, ESCs and battery leads, and calibrate it.
3. Tilt the nose up: `pitch` goes **positive**. Roll right side down: `roll` goes **positive**.
   Flip signs in `attitude()` if needed.
4. Lift the board 1 m: `alt_agl` rises about 1 m. GPS altitude is noisy (±several m), and that
   error feeds straight into position error, so use a rangefinder if you can.
5. On the Jetson, run with `telemetry.kind: serial`. `telemetry_log.csv` fills up. **Keep these
   logs**: together with a recorded clip they let you replay a real flight indoors
   (`source.kind: video` + `telemetry.kind: replay`).

If the drone has a flight controller (ArduPilot/PX4), it already has a better GPS, compass
and IMU. Reading its MAVLink telemetry (`pymavlink`) and writing the same JSON line is the
better route. Only `telemetry.py` would change.

## 4. Clock alignment

The Jetson has no RTC battery, so its clock can be wrong after boot. The serial reader
estimates `GPS time - Jetson time` and live frames are stamped on the GPS clock. For
prerecorded clips, set `video_start_time` to the GPS time of the first frame. A good trick
is to film the ESP32 serial output, or a phone showing GPS time, at the start of the recording.

## 5. Field accuracy test (do this before judging)

1. Mark 5–10 spots on a field and record their coordinates (phone GPS averaged over a minute,
   or better, measured offsets from one reference point). Have volunteers stand on them.
2. Fly or hold the drone at a known height over them and record video + telemetry.
3. Put the spots in `ground_truth.csv` (`truth_id,lat,lon`), process the clip, run
   `scripts/evaluate.py`. Quote the median/p90 error and match counts in the pitch.

If full flight isn't possible: a fixed-height rig (pole or balcony) pointing down is fine.
Label it as a demo rig.
