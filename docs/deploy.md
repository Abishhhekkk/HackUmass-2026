# Cloud deployment (Vultr + TigerData)

The cloud side is optional. The drone and local dashboard work without it. It is one small
FastAPI process.

## TigerData (Postgres + PostGIS)

1. Create a Tiger Cloud service and copy its connection string.
2. Enable PostGIS once (`CREATE EXTENSION postgis;`). The API also tries this at startup.
3. `pip install "psycopg[binary]"` and set
   `DATABASE_URL=postgresql://USER:PASS@HOST:PORT/tsdb?sslmode=require`.
4. Start the API once. It applies `infra/db/tiger_schema.sql` (idempotent).
5. **Check:** `GET /api/v1/health` reports `"db":"postgres"`, and after an upload
   `/api/v1/missions/<id>/people/near?lat=..&lon=..&radius_m=100` returns rows.
   This query runs through PostGIS `ST_DWithin`.

Without `DATABASE_URL` the API uses SQLite at `data/cloud/rescue.db`. That's the fallback if
sponsor access fails.

Honest framing for the sponsor: the data is time-stamped and geospatial (people with
first/last-seen times, snapshot versions over time). PostGIS does the spatial queries for
the map and the agent. Time-series features such as hypertables become useful once you
store raw telemetry or many missions. Don't claim more than that.

## Vultr

A small Cloud Compute instance (1 vCPU / 1–2 GB, Ubuntu) is plenty. No GPU is needed: inference runs on the Jetson.

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv git
git clone <your repo> rescue-sight && cd rescue-sight
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt "psycopg[binary]"      # + server/requirements-ai.txt for Gemini
cp .env.example .env && nano .env                             # set INGEST_API_KEY, DATABASE_URL, GEMINI_API_KEY
set -a && . ./.env && set +a
cd server && uvicorn rescue_api.app:app --host 0.0.0.0 --port 8000
```

For the demo, run it under `tmux` or a systemd unit. Put Caddy in front for HTTPS
(`caddy reverse-proxy --from your-domain --to :8000`), or open port 8000 in the Vultr
firewall. Set the same `INGEST_API_KEY` on the Jetson and point `uploader.server_url` at the
instance.

Snapshot files live in `data/cloud/snapshots`. Vultr Object Storage (S3-compatible) is the
natural next step for those files and for the later video/image upload. It isn't wired in yet.

## Gemini report (optional)

`pip install -r server/requirements-ai.txt`, set `GEMINI_API_KEY` (and optionally
`GEMINI_MODEL`, default `gemini-2.5-flash`. Check this is still a current model name). The
**AI summary** button on the cloud dashboard calls `POST /api/v1/missions/<id>/ai-report`.
Without a key or network it returns the built-in report and says why.
