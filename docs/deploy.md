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
3. Install the optional Postgres driver with `pip install -r server/requirements-postgres.txt` and configure
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
pip install -r server/requirements.txt -r server/requirements-postgres.txt
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

## Gemini report

Install `server/requirements-ai.txt` and set `GEMINI_API_KEY` (optionally `GEMINI_MODEL`, default
`gemini-2.5-flash`; verify that model is available for your key). For local development, the
backend reads those values from the repository-root `.env` file or process environment; Vite
does not load or bundle the root file.

Reports are **periodic or on demand, never per upload** (the edge can send a snapshot every
~10 s; one LLM call per snapshot would be slow, costly and redundant):

| Setting | Default | Meaning |
|---|---|---|
| `REPORT_INTERVAL_S` | `300` | Every this many seconds, report on each mission whose newest snapshot has no report yet. `0` = on demand only. |
| `REPORT_MIN_GAP_S` | `20` | Minimum time between reports for one mission; the dashboard button gets `429` with `retry_after_s` if pressed sooner. |

* `GET /api/v1/missions/<id>/ai-report` returns the newest stored report (it never triggers
  generation), with `snapshot_version`, `latest_snapshot_version` and `stale: true` when newer
  data has arrived since. The dashboard shows that.
* `POST /api/v1/missions/<id>/ai-report` (the *Generate fresh summary* button) reports on the
  newest snapshot immediately, subject to `REPORT_MIN_GAP_S`.
* Each report is stored next to the snapshot it describes (`vNNNNNN/ai-report.json`, `.md`).

The report uses only the allow-listed, read-only mission tools (frame-aligned drone GPS,
sightings, data gaps, OpenStreetMap landmarks). If Gemini is unavailable (no key, no network,
an error), the deterministic report is stored instead and labelled as such. Mission Q&A is
`POST /api/v1/missions/<id>/ask`. Reports run in the cloud only and never affect onboard
inference or snapshot creation.
