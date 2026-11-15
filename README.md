# Vantage | HackUMass 2026

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

The site has two pages: the home page (`/`) shows a replay of the live mission, the offline
link with its snapshot versions, measured detector results and a mission-scoped assistant
(cloud only); mission control (`/#/dashboard`) is the operator console: map, readings, sightings
without a position, exports and reports. Outage and upload-integrity demo controls appear when
the edge has an uplink configured. Gemini Q&A is read-only
and falls back to the deterministic report when credentials or the integration are unavailable.

## One-click start (demo)

Put the same `.env` (DATABASE_URL, INGEST_API_KEY, optional GEMINI_API_KEY) in the repo root on
both machines, then:

* **Laptop (cloud side):** double-click `Vantage-Cloud.bat`. It installs what changed, rebuilds the
  website if needed, starts the API (refuses to run without Tiger Data or a real ingest key),
  starts the Cloudflare quick tunnel and records its address in Tiger Data.
* **Jetson (drone side):** `./vantage-edge.sh` (or `./vantage-edge.sh --install-shortcut` once for
  a desktop icon). It reads the laptop's address from Tiger Data and keeps following it, so a
  restarted tunnel needs no reconfiguration; uses the trained engine and BoT-SORT; runs the
  newest clip config, or `--video clip.mp4` (its `.SRT` flight log if present, else a labelled
  simulated path). With no address yet the mission still runs and uploads start later.
  `CLOUD_URL=https://...` in the Jetson's `.env` forces an address instead.

## Cloud: FastAPI + Tiger Data

The Jetson uploads snapshots over any internet link (4G/5G stick, hotspot, Wi-Fi) to a
stateless FastAPI service that stores everything in Tiger Data. For the hackathon it runs on a
laptop behind a free Cloudflare quick tunnel; the same container runs on any cloud host. Steps
and an end-to-end check (`scripts/check_cloud.py`): [docs/deploy.md](./docs/deploy.md).

## Recorded footage from an existing drone

Vantage adds onboard AI and offline storage to drones that have neither: a Jetson Orin
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
