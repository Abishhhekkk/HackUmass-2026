# RescueSight | HackUMass 2026

Offline-first search-and-rescue situational awareness prototype. A Jetson Orin Nano processes
the drone's video feed with onboard person detection, while ESP32 GPS/pose telemetry is aligned
to frames to generate estimated coordinates and a heatmap locally. Versioned, compact mission
packages stay on the aircraft until a link returns; a ground station or cloud connection is not
required for onboard processing. The React dashboard can run against either the edge API or
cloud API. All locations and counts are estimates for human review, not confirmed people or
rescue priorities.

For Jetson engine export, live inference, and the ESP32 serial line format, see
[docs/jetson-live-inference.md](./docs/jetson-live-inference.md).

## Run the software demo

Requirements: Python 3.10+, Node.js 20+, and npm.

1. Install the project dependencies:

   ```powershell
   python -m pip install -r server/requirements.txt
   python -m pip install -r edge/requirements.txt
   Set-Location web
   npm ci
   npm run build
   Set-Location ..
   ```

2. Start the scripted mock edge/cloud demo:

   ```powershell
   python scripts/demo_outage.py
   ```

   It prints the dashboard URLs and runs a simulated connectivity outage and recovery. The
   cloud API uses SQLite by default, so no database or API key is required for the local demo.

Click the RescueSight wordmark in the dashboard to open the interactive project overview. The
overview uses live mission snapshots, exposes edge-only outage and upload-integrity controls,
and provides a mission-scoped assistant when served by the cloud API. Gemini Q&A is read-only
and falls back to the deterministic report when credentials or the integration are unavailable.

## Cloud: FastAPI + Tiger Data

The Jetson uploads snapshots over any internet link (4G/5G stick, hotspot, Wi-Fi) to a
stateless FastAPI service that stores everything in Tiger Data. For the hackathon it runs on a
laptop behind a free Cloudflare quick tunnel; the same container runs on any cloud host. Steps
and an end-to-end check (`scripts/check_cloud.py`): [docs/deploy.md](./docs/deploy.md).

## Recorded footage from an existing drone

RescueSight adds onboard AI and offline storage to drones that have neither: a Jetson Orin
Nano processes the drone's video together with the drone's own flight log (DJI `.SRT`).
Put the video and its `.SRT` in `data/footage/`, check the log with
`python scripts/srt_to_telemetry.py data/footage/DJI_0001.SRT --utc-offset -5`, then run
`python -m rescue_edge.main --config ../config/edge.footage.yaml --serve` from `edge/`
(steps in the config's header). Detection uses tiled YOLO (`docs/detector-eval.md`); to train
an aerial person model on a laptop or Vultr GPU and load it on the Jetson, see
[docs/training.md](./docs/training.md).

For frontend development, start the API first, then run `npm run dev` in `web`. See
[docs/deploy.md](./docs/deploy.md) for dashboard builds, TigerData/PostGIS configuration, and
Gemini setup. For actual Jetson hardware, use [docs/hardware.md](./docs/hardware.md).
