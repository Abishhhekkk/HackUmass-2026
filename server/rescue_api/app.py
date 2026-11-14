"""Cloud API (host on Vultr). Receives snapshots from the Jetson and serves the dashboard.

    uvicorn rescue_api.app:app --host 0.0.0.0 --port 8000      (from server/)

Env: INGEST_API_KEY, DATABASE_URL (sqlite:///... or postgresql://...), SNAPSHOT_DIR,
     GEMINI_API_KEY, GEMINI_MODEL, WEB_DIR, MAX_UPLOAD_BYTES
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from .storage import Storage

REPO = Path(__file__).resolve().parents[2]
ALLOWED_FILES = {"sightings.csv", "heatmap.geojson", "report.md"}
ID_RE = r"^[A-Za-z0-9._-]{1,80}$"


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_app(storage: Storage | None = None) -> FastAPI:
    app = FastAPI(title="RescueSight cloud API")
    store = storage or Storage(os.environ.get("DATABASE_URL", f"sqlite:///{REPO / 'data/cloud/rescue.db'}"),
                               os.environ.get("SNAPSHOT_DIR", str(REPO / "data/cloud/snapshots")))
    app.state.store = store
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
    async def ingest(manifest: UploadFile = File(...), files: list[UploadFile] = File(default=[])):
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
            store.insert_snapshot(cur, manifest_dict, received, got["sightings.csv"])
        return JSONResponse({"status": "accepted", "latest_version": m.version, "received_at": received},
                            status_code=201)

    def _row(mission_id: str, version: int | None = None) -> dict:
        if not re.match(ID_RE, mission_id):
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
        return FileResponse(_path(mission_id, row["version"], name), filename=f"{mission_id}-{name}")

    @app.get("/api/v1/missions/{mission_id}/report", response_class=PlainTextResponse)
    def report(mission_id: str):
        row = _row(mission_id)
        return _path(mission_id, row["version"], "report.md").read_text()

    @app.get("/api/v1/missions/{mission_id}/people/near")
    def near(mission_id: str, lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180),
             radius_m: float = Query(100, gt=0, le=2000)):
        _row(mission_id)
        return store.people_near(mission_id, lat, lon, radius_m)

    @app.post("/api/v1/missions/{mission_id}/ai-report")
    def ai_report(mission_id: str):
        from .agent import generate_report

        row = _row(mission_id)
        deterministic = _path(mission_id, row["version"], "report.md").read_text()
        return generate_report(store, mission_id, row, deterministic)

    web = Path(os.environ.get("WEB_DIR", REPO / "web"))
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


app = create_app() if os.environ.get("RESCUE_API_NO_AUTOCREATE") != "1" else None
