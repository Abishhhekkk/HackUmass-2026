# Cloud deployment (Vultr + TigerData)

The cloud side is optional. The drone and local dashboard continue to work without it. The
cloud service is a small FastAPI process.

## Build the dashboard

The React dashboard is built once and served as static files by either the edge or cloud API:

```bash
cd web
npm ci
npm run build
cd ..
```

The build is written to `web/dist`. Both API servers automatically serve that build when it
exists. For UI development, start the cloud API (`uvicorn rescue_api.app:app --reload --port
8000` from `server/`), run the edge service on port 8080, then run `npm run dev` in `web/`. Vite
proxies `/api` to the cloud API and `/edge-api` to the local edge API, so mission reports stay
cloud-backed while story-page controls can reach the edge uploader. Without a local edge service,
those controls run as an explicitly labeled visual preview. To point all API requests at a single
service instead, set `VITE_API_PROXY` before starting Vite. Outage toggles require the edge
uploader's `offline_flag_file`. The production dashboard uses same-origin `/api/v1` routes when
served by FastAPI; real edge controls should be used from the edge dashboard.

## TigerData (Postgres + PostGIS)

1. Create a Tiger Cloud service and copy its connection string.
2. Enable PostGIS (`CREATE EXTENSION postgis;`) if it is not already enabled. The API also tries
   to enable it at startup.
3. Install the optional Postgres driver with `pip install "psycopg[binary]>=3.1"` and configure
   `DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/tsdb?sslmode=require` with your service's
   connection details. Keep real credentials out of Git and frontend code.
4. Start the API. It applies `infra/db/tiger_schema.sql` (idempotent).
5. Check that `GET /api/v1/health` reports `"db":"postgres"`. After an upload,
   `/api/v1/missions/<id>/people/near?lat=..&lon=..&radius_m=100` should return rows using
   PostGIS `ST_DWithin`.

Without `DATABASE_URL`, the API uses SQLite at `data/cloud/rescue.db`. Snapshot files are stored
on the server filesystem under `data/cloud/snapshots`; Vultr Object Storage is not wired in.

The current cloud data includes time-stamped located sightings, unlocated track summaries, and
aggregated heatmap-cell polygons from each accepted snapshot, alongside versioned snapshot
metadata. PostGIS powers spatial queries for the map and agent. Raw video is not uploaded or
stored in TigerData. Hypertables may be useful later if the project begins storing high-volume
telemetry; they are not currently used.

## Vultr

A small Cloud Compute instance (1 vCPU / 1–2 GB, Ubuntu) is sufficient; inference runs on the
Jetson, not the cloud.

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone <your repo> rescue-sight && cd rescue-sight
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt "psycopg[binary]>=3.1"
pip install -r server/requirements-ai.txt  # optional Gemini integration
cd web && npm ci && npm run build && cd ..
cp .env.example .env && nano .env
set -a && . ./.env && set +a
cd server && uvicorn rescue_api.app:app --host 0.0.0.0 --port 8000
```

Set `INGEST_API_KEY`, `DATABASE_URL`, and (if enabled) `GEMINI_API_KEY` in the environment.
Run under `tmux` or a systemd unit. Put Caddy in front for HTTPS, or secure the service with
appropriate firewall rules. Set the same ingest key on the Jetson and point its uploader at
the instance.

## Gemini report (optional)

Install `server/requirements-ai.txt` and set `GEMINI_API_KEY` (optionally `GEMINI_MODEL`, default
`gemini-2.5-flash`; verify that model is available for your key). For local development, the
backend reads those values from the repository-root `.env` file or process environment; Vite
does not load or bundle the root file. The cloud dashboard's AI summary calls
After a snapshot upload is accepted, the cloud API automatically schedules a mission report.
The dashboard polls `GET /api/v1/missions/<id>/ai-report` and presents it when ready; the report
uses the allow-listed, read-only mission tools, including frame-aligned drone GPS, sightings,
data gaps, OpenStreetMap landmarks, and hazard-relevant features near the densest cluster. Set `HAZARD_TYPE` (`fire`, `flood` or
`general`, default `general`) to choose what to look for: for `fire`, the nearest water body, open
ground and major roads; for `flood`, water, shelters, bridges and roads. A separate Gemini call
grounded in Google Maps (`google-genai` SDK; model `GEMINI_MAPS_MODEL`, default `GEMINI_MODEL`)
finds the places; distance and compass direction are then computed locally from the coordinates it
returns, and implausible ones are dropped. Only if Gemini is unavailable does it fall back to
OpenStreetMap (Overpass), measuring to the nearest mapped shoreline or road point. The report says
which source was used. Check that Maps grounding is enabled and billed for your key, and show the
returned Google Maps source links wherever the report is displayed. This is reference information,
not firefighting or evacuation advice. Without an available Gemini integration/key, it records
a deterministic report instead. Mission Q&A calls `POST /api/v1/missions/<id>/ask`; the
on-demand report endpoint is `POST /api/v1/missions/<id>/ai-report`. Reports are optional cloud
outputs and do not participate in onboard inference or snapshot creation.
