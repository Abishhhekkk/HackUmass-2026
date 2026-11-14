"""Cloud API (host on Vultr). Receives snapshots from the Jetson and serves the dashboard.

    uvicorn rescue_api.app:app --host 0.0.0.0 --port 8000      (from server/)

Env: INGEST_API_KEY, DATABASE_URL (sqlite:///... or postgresql://...), SNAPSHOT_DIR,
     GEMINI_API_KEY, GEMINI_MODEL, WEB_DIR, MAX_UPLOAD_BYTES,
     REPORT_INTERVAL_S (default 300, 0 = on demand only), REPORT_MIN_GAP_S (default 20)
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from .report_manager import ReportManager, ReportRateLimited
from .storage import Storage

REPO = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)
NOT_BUILT_HTML = """<!doctype html><meta charset="utf-8"><title>RescueSight: dashboard not built</title>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem">
<h1>Dashboard not built</h1>
<p>The API is running, but <code>web/dist</code> does not exist, so there is no dashboard to serve.
Build it once from the repository root:</p>
<pre>cd web
npm ci
npm run build</pre>
<p>Then reload this page (no restart needed). The API itself works now, e.g.
<a href="/api/v1/status">/api/v1/status</a>.</p></body>"""

ALLOWED_FILES = {"sightings.csv", "heatmap.geojson", "report.md", "telemetry.json", "ai-report.md",
                 "flight_path.csv"}  # flight_path.csv is optional (older edges don't send it)
REQUIRED_FILES = {"sightings.csv", "heatmap.geojson", "report.md"}  # ingest and reads depend on them
ID_RE = r"^[A-Za-z0-9._-]{1,80}$"
RESERVED_IDS = {".", ".."}  # match ID_RE but are directory references, not names


def valid_id(value: str) -> bool:
    """IDs become directory names under SNAPSHOT_DIR, so "." and ".." are refused."""
    return bool(re.match(ID_RE, value)) and value not in RESERVED_IDS


class FileEntry(BaseModel):
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(ge=0)


class Manifest(BaseModel, extra="allow"):
    schema_version: int = Field(ge=1, le=1)
    mission_id: str = Field(pattern=ID_RE)
    drone_id: str = Field(pattern=ID_RE)
    version: int = Field(ge=1)
    created_at: str
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: dict
    files: list[FileEntry]

    @field_validator("mission_id", "drone_id")
    @classmethod
    def id_must_not_be_dot_path(cls, value: str) -> str:
        if value in RESERVED_IDS:
            raise ValueError("'.' and '..' are not valid IDs")
        return value


class MissionQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value.strip()


def flight_path_geojson(rows: list[dict]) -> dict:
    """Valid fixes become LineString segments; a telemetry gap (invalid/stale) ends a segment
    instead of being drawn across. The drone's track is never a sighting location."""
    segments, current, gaps = [], [], 0
    for r in rows:
        if r["valid"] and r["lat"] is not None:
            current.append([round(r["lon"], 7), round(r["lat"], 7)])
            continue
        gaps += 1
        if len(current) >= 2:
            segments.append(current)
        current = []
    if len(current) >= 2:
        segments.append(current)
    valid = [r for r in rows if r["valid"] and r["lat"] is not None]
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": seg},
                      "properties": {"kind": "drone_path"}} for seg in segments],
        "properties": {
            "samples": len(rows), "gap_samples": gaps,
            "start": rows[0]["observed_at"] if rows else None,
            "end": rows[-1]["observed_at"] if rows else None,
            "last_valid": ({"lat": valid[-1]["lat"], "lon": valid[-1]["lon"],
                            "observed_at": valid[-1]["observed_at"]} if valid else None),
            "note": "Drone GPS track, not sighting locations.",
        },
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_app(storage: Storage | None = None) -> FastAPI:
    store = storage or Storage(os.environ.get("DATABASE_URL", f"sqlite:///{REPO / 'data/cloud/rescue.db'}"),
                               os.environ.get("SNAPSHOT_DIR", str(REPO / "data/cloud/snapshots")))
    # Reports are periodic or on demand, never per upload (see report_manager.py).
    reports = ReportManager(store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        reports.start()  # no-op when REPORT_INTERVAL_S=0
        try:
            yield
        finally:
            reports.stop()

    app = FastAPI(title="RescueSight cloud API", lifespan=lifespan)
    app.state.store = store
    app.state.reports = reports
    api_key = os.environ.get("INGEST_API_KEY")
    max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))

    def require_key(x_api_key: Annotated[str | None, Header()] = None):
        if api_key and not (x_api_key and hmac.compare_digest(x_api_key, api_key)):
            raise HTTPException(401, "bad or missing X-API-Key")

    @app.get("/api/v1/health")
    def health():
        return {"ok": True, "db": "postgres" if store.pg else "sqlite"}

    @app.get("/api/v1/status")
    def status():
        return {"role": "cloud", "db": "postgres" if store.pg else "sqlite"}

    @app.post("/api/v1/snapshots", dependencies=[Depends(require_key)])
    async def ingest(
        manifest: UploadFile = File(...),
        files: list[UploadFile] = File(default=[]),
    ):
        raw = await manifest.read()
        try:
            m = Manifest.model_validate_json(raw)
        except ValidationError as e:
            raise HTTPException(422, f"invalid manifest: {e.errors()[:3]}")
        got: dict[str, bytes] = {}
        total = len(raw)
        for f in files:
            data = await f.read()
            total += len(data)
            if total > max_bytes:
                raise HTTPException(413, "snapshot too large")
            got[f.filename] = data
        expected = {fe.name: fe for fe in m.files}
        if set(expected) != set(got) or not set(got) <= ALLOWED_FILES:
            raise HTTPException(422, f"file set mismatch: expected {sorted(expected)}, got {sorted(got)}")
        if missing := REQUIRED_FILES - set(expected):
            raise HTTPException(422, f"snapshot is missing required files: {sorted(missing)}")
        for name, fe in expected.items():
            if len(got[name]) != fe.bytes or hashlib.sha256(got[name]).hexdigest() != fe.sha256:
                # most likely a transfer cut short; the Jetson will retry
                raise HTTPException(422, f"checksum mismatch for {name}")
        manifest_dict = json.loads(raw)

        with store.tx() as cur:
            current, current_hash = store.latest(cur, m.mission_id)
            if m.version < current:
                return {"status": "stale", "latest_version": current}
            if m.version == current:
                if current_hash == m.content_hash:
                    return {"status": "duplicate", "latest_version": current}
                raise HTTPException(409, "same version with different content")
            d = store.snapshot_dir / m.mission_id / f"v{m.version:06d}"
            tmp = d.with_name(d.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            for name, data in got.items():
                (tmp / name).write_bytes(data)
            (tmp / "manifest.json").write_bytes(raw)
            shutil.rmtree(d, ignore_errors=True)
            tmp.rename(d)
            received = now_iso()
            store.insert_snapshot(
                cur, manifest_dict, received, got["sightings.csv"], got["heatmap.geojson"]
            )
            store.insert_flight_path(cur, manifest_dict, got.get("flight_path.csv"))
        return JSONResponse({"status": "accepted", "latest_version": m.version, "received_at": received},
                            status_code=201)

    def _row(mission_id: str, version: int | None = None) -> dict:
        if not valid_id(mission_id):
            raise HTTPException(404)
        row = store.snapshot_row(mission_id, version)
        if not row:
            raise HTTPException(404, "no snapshot for mission")
        return row

    def _path(mission_id: str, version: int, name: str) -> Path:
        return store.snapshot_dir / mission_id / f"v{version:06d}" / name

    @app.get("/api/v1/missions")
    def missions():
        return store.missions()

    @app.get("/api/v1/missions/{mission_id}/latest")
    def latest(mission_id: str):
        row = _row(mission_id)
        heat = json.loads(_path(mission_id, row["version"], "heatmap.geojson").read_text())
        return {"manifest": row["manifest"], "heatmap": heat, "received_at": row["received_at"],
                "source": "cloud"}

    @app.get("/api/v1/missions/{mission_id}/files/{name}")
    def file(mission_id: str, name: str, version: int | None = None):
        if name not in ALLOWED_FILES | {"manifest.json"}:
            raise HTTPException(404)
        row = _row(mission_id, version)
        file_path = _path(mission_id, row["version"], name)
        if not file_path.is_file():
            raise HTTPException(404, f"{name} is not present in this snapshot")
        return FileResponse(file_path, filename=f"{mission_id}-{name}")

    @app.get("/api/v1/missions/{mission_id}/report", response_class=PlainTextResponse)
    def report(mission_id: str):
        row = _row(mission_id)
        return _path(mission_id, row["version"], "report.md").read_text()

    @app.get("/api/v1/missions/{mission_id}/ai-report")
    def automated_report(mission_id: str):
        """Newest stored report. Reading never triggers generation."""
        row = _row(mission_id)
        schedule = reports.status(mission_id)
        stored = reports.latest_report(mission_id)
        if stored is None:
            return {
                "mission_id": mission_id,
                "snapshot_version": None,
                "latest_snapshot_version": row["version"],
                "status": "pending" if reports.periodic else "none",
                "report": None,
                "generated_by": None,
                "human_review_required": True,
                "schedule": schedule,
            }
        return {**stored, "latest_snapshot_version": row["version"],
                "stale": (stored.get("snapshot_version") or 0) < row["version"],
                "schedule": schedule}

    @app.get("/api/v1/missions/{mission_id}/flight-path")
    def flight_path(mission_id: str):
        """Drone track from the drone_telemetry time-series table, as GeoJSON."""
        _row(mission_id)
        return flight_path_geojson(store.flight_path(mission_id))

    @app.get("/api/v1/missions/{mission_id}/people/near")
    def near(mission_id: str, lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
             radius_m: float = Query(100, gt=0, le=2000)):
        _row(mission_id)
        return store.people_near(mission_id, lat, lon, radius_m)

    @app.post("/api/v1/missions/{mission_id}/ai-report")
    def ai_report(mission_id: str):
        """On demand (dashboard button): report on the newest snapshot now, rate-limited."""
        row = _row(mission_id)
        try:
            result = reports.generate(mission_id, row["version"])
        except ReportRateLimited as e:
            return JSONResponse(
                {"detail": f"{e.reason}; try again in {e.retry_after_s} s",
                 "retry_after_s": e.retry_after_s},
                status_code=429, headers={"Retry-After": str(e.retry_after_s)},
            )
        return {**result, "latest_snapshot_version": row["version"], "stale": False,
                "schedule": reports.status(mission_id)}

    @app.get("/api/v1/ai/status")
    def ai_status():
        from .agent import _ai_settings

        ai = _ai_settings()
        return {
            "configured": bool(ai["GEMINI_API_KEY"]),
            "integration_installed": importlib.util.find_spec("langchain_google_genai") is not None,
            "model": ai["GEMINI_MODEL"],
            "tools": [
                "get_mission_summary",
                "get_latest_drone_gps",
                "get_recent_sightings",
                "get_sightings_near_drone",
                "get_unlocated_sightings",
                "get_densest_cells",
                "get_people_near",
                "find_nearby_landmarks",
            ],
        }

    @app.post("/api/v1/missions/{mission_id}/ask")
    def ask_mission(mission_id: str, request: MissionQuestion):
        from .agent import answer_question

        row = _row(mission_id)
        deterministic = _path(mission_id, row["version"], "report.md").read_text()
        return answer_question(store, mission_id, row, deterministic, request.question)

    # WEB_DIR is the web/ project folder; its dist/ build is what gets served.
    dist = Path(os.environ.get("WEB_DIR", REPO / "web")) / "dist"
    static = StaticFiles(directory=dist, html=True, check_dir=False)

    async def dashboard(scope, receive, send):
        # web/index.html loads /src/main.jsx, which a browser cannot run, so without a
        # build show how to build instead of a blank page. Checked per request.
        if (dist / "index.html").is_file():
            await static(scope, receive, send)
        else:
            await HTMLResponse(NOT_BUILT_HTML, status_code=503)(scope, receive, send)

    app.mount("/", dashboard, name="web")
    return app


app = create_app() if os.environ.get("RESCUE_API_NO_AUTOCREATE") != "1" else None
