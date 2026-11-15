# Cloud deployment: Jetson → FastAPI → Tiger Data

The Jetson works with no connection at all. Whenever it has *any* internet route (a 4G/5G stick,
a phone hotspot, Wi-Fi when it lands), its uploader sends the newest snapshot over **outgoing
HTTPS** to the cloud API, which stores everything in **Tiger Data**:

```
Jetson (offline-first) ── HTTPS out, whenever a link exists ──▶ FastAPI ──▶ Tiger Data
                                                         (public URL)    (all mission data)
```

The API is **stateless**: snapshot files, indexed rows and AI reports all live in the database
(`server/rescue_api/storage.py`), nothing on the server's disk. So the same API runs on a laptop
behind a free tunnel, in a container, or on Vultr, with no data loss when it restarts. The
database password stays with the API, never on the drone. Mobile networks do not let anything
connect *in* to the drone, which is why the Jetson only ever calls out.

## Option A (hackathon setup): your laptop + a Cloudflare quick tunnel

The laptop runs the API; a free Cloudflare quick tunnel gives it a public HTTPS address
(`https://<random>.trycloudflare.com`) that the Jetson reaches over any internet link. No
Cloudflare account, domain or port forwarding needed. Commands are for Windows PowerShell in
the repo folder with the venv active (`git checkout claude && git pull`).

### 1. Tiger Data
In the Tiger Cloud console, create (or open) a service and copy its **connection string**
(`postgresql://USER:PASSWORD@HOST:PORT/tsdb?sslmode=require`). The API creates its tables
itself at start, turns `drone_telemetry` into a hypertable, and enables PostGIS. If start-up
fails with a permission error about `postgis`, run `CREATE EXTENSION IF NOT EXISTS postgis;`
once in the console's SQL editor.

### 2. API settings (`.env` in the repo root, never committed)
```powershell
pip install -r server/requirements.txt -r server/requirements-postgres.txt
copy .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
notepad .env
```
In `.env`: set `INGEST_API_KEY` to the printed key and `DATABASE_URL` to the connection string
(uncomment it). The key is mandatory once the API is public: uploads without it are refused.
Optional: `pip install -r server/requirements-ai.txt` and `GEMINI_API_KEY` for AI reports.

### 3. Dashboard (optional, needs Node.js LTS)
```powershell
cd web; npm ci; npm run build; cd ..
```

### 4. Start the API (window 1, keep open)
```powershell
cd server; uvicorn rescue_api.app:app --host 127.0.0.1 --port 8000
```
`127.0.0.1` is enough (the tunnel connects locally; no Windows firewall prompt). Check
http://127.0.0.1:8000/api/v1/health shows `"db":"postgres"`, then (window 2, repo root):
```powershell
python scripts/check_cloud.py --url http://127.0.0.1:8000 --cleanup
```
It uploads a small `check-…` mission with the Jetson's uploader code, reads it back through the
API and counts the rows in Tiger Data (key and `DATABASE_URL` come from `.env`). Expect
`ALL CHECKS PASSED`, including "drone_telemetry is a TimescaleDB hypertable".

Verified 2026-11-15 against a real Tiger Data service (Postgres 18.6, TimescaleDB installed,
PostGIS enabled by the API at start): 12/12 checks passed, including the hypertable.

### 5. Public address (window 3, keep open)
```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```
(Open a new window after installing.) It prints `https://<random>.trycloudflare.com`. Check it:
```powershell
python scripts/check_cloud.py --url https://<random>.trycloudflare.com --cleanup
```
The address changes every time `cloudflared` restarts; update the Jetson when it does. Quick
tunnels are meant for testing; for a fixed address, use a named tunnel (free Cloudflare account
plus a domain on Cloudflare) or a cloud host (Option B/C). Anyone with the address can *view*
the dashboard and missions (uploads need the key): fine for a demo, not for real data.

### 6. Jetson
```bash
cd ~/HackUmass-2026 && git pull && source ~/rescue-venv/bin/activate
export INGEST_API_KEY='<same key as the laptop .env>'
python scripts/check_cloud.py --url https://<random>.trycloudflare.com --cleanup
```
Without `DATABASE_URL` the Jetson checks only through the API, which is the point: it has no
database credentials. Then set `uploader.server_url: https://<random>.trycloudflare.com` in your
config (e.g. `config/<clip>.sim.yaml` or `config/edge.footage.yaml`) and run the pipeline with
the same `INGEST_API_KEY` exported. Snapshots appear in Tiger Data within seconds of each
snapshot (about every 10 s of flight). Turning the Jetson's hotspot off and on shows the
offline capture and catch-up; the edge dashboard's "cut uplink" control simulates it without
touching Wi-Fi.

## Option B: container (any host)

`Dockerfile` builds the dashboard and the API into one image (~360 MB):
```bash
docker build -t rescuesight-api .
docker run --rm -p 8000:8000 --env-file .env rescuesight-api
```
Hosts that assign a port set `$PORT`. Because the API is stateless, free container hosts that
sleep or wipe their disk (Render, Hugging Face Spaces, Google Cloud Run, …) work; the uploader
simply retries while a sleeping host wakes up. Set `INGEST_API_KEY` and `DATABASE_URL` as the
host's secrets, then point the Jetson's `server_url` at the host's HTTPS address.

## Option C: Vultr (or any Ubuntu VM)

A small CPU instance is enough (inference runs on the Jetson). Either run the container
(Option B; install Docker first) or, without Docker:
```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/Abishhhekkk/HackUmass-2026.git && cd HackUmass-2026 && git checkout claude
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt -r server/requirements-postgres.txt
cp .env.example .env && nano .env        # INGEST_API_KEY + DATABASE_URL
cd server && uvicorn rescue_api.app:app --host 127.0.0.1 --port 8000
```
Expose it with HTTPS: `cloudflared tunnel --url http://127.0.0.1:8000` works here too, or a
reverse proxy such as Caddy. Run it under `tmux` or a systemd service so it survives logout.

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
