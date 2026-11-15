"""Cloud API (host on Vultr). Receives snapshots from the Jetson and serves the dashboard.

    uvicorn rescue_api.app:app --host 0.0.0.0 --port 8000      (from server/)

Env: INGEST_API_KEY, DATABASE_URL (sqlite:///... or postgresql://...), SNAPSHOT_DIR,
     GEMINI_API_KEY, GEMINI_MODEL, WEB_DIR, MAX_UPLOAD_BYTES
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
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, field_validator

from .storage import Storage

REPO = Path(__file__).resolve().parents[2]
log = logging.getLogger(__name__)
ALLOWED_FILES = {"sightings.csv", "heatmap.geojson", "report.md", "telemetry.json", "ai-report.md"}
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


class MissionQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=1000)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value.strip()


def _generate_automated_report(store: Storage, mission_id: str, version: int) -> None:
    """Create the post-upload report without holding up the drone's upload response."""
    row = store.snapshot_row(mission_id, version)
    if not row:
        log.error("cannot generate report for missing mission snapshot %s v%d", mission_id, version)
        return
    directory = store.snapshot_dir / mission_id / f"v{version:06d}"
    try:
        deterministic = (directory / "report.md").read_text(encoding="utf-8")
        from .agent import generate_report

        result = generate_report(store, mission_id, row, deterministic)
        report_text = result["report"]
        (directory / "ai-report.md").write_text(report_text, encoding="utf-8")
        (directory / "ai-report.json").write_text(
            json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        log.info(
            "automated report ready for %s v%d (%s)",
            mission_id, version, result.get("generated_by", "unknown"),
        )
    except Exception:
        log.exception("automated report generation failed for %s v%d", mission_id, version)
        try:
            (directory / "ai-report.json").write_text(
                json.dumps({
                    "mission_id": mission_id,
                    "snapshot_version": version,
                    "status": "failed",
                    "generated_by": None,
                    "report": None,
                    "error": "Report generation failed; check cloud service logs.",
                    "human_review_required": True,
                }, separators=(",", ":")),
                encoding="utf-8",
            )
        except OSError:
            log.exception("could not persist report failure state for %s v%d", mission_id, version)


def _run_scheduled_report(store: Storage, mission_id: str, version: int, jobs: set, lock: threading.Lock) -> None:
    key = (mission_id, version)
    try:
        _generate_automated_report(store, mission_id, version)
    finally:
        with lock:
            jobs.discard(key)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_app(storage: Storage | None = None) -> FastAPI:
    app = FastAPI(title="RescueSight cloud API")
    store = storage or Storage(os.environ.get("DATABASE_URL", f"sqlite:///{REPO / 'data/cloud/rescue.db'}"),
                               os.environ.get("SNAPSHOT_DIR", str(REPO / "data/cloud/snapshots")))
    app.state.store = store
    api_key = os.environ.get("INGEST_API_KEY")
    max_bytes = int(os.environ.get("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))
    report_jobs: set[tuple[str, int]] = set()
    report_jobs_lock = threading.Lock()

    def schedule_report(background_tasks: BackgroundTasks, mission_id: str, version: int) -> None:
        key = (mission_id, version)
        with report_jobs_lock:
            if key in report_jobs:
                return
            report_jobs.add(key)
        background_tasks.add_task(
            _run_scheduled_report, store, mission_id, version, report_jobs, report_jobs_lock
        )

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
        background_tasks: BackgroundTasks,
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
        schedule_report(background_tasks, m.mission_id, m.version)
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
        file_path = _path(mission_id, row["version"], name)
        if not file_path.is_file():
            raise HTTPException(404, f"{name} is not present in this snapshot")
        return FileResponse(file_path, filename=f"{mission_id}-{name}")

    @app.get("/api/v1/missions/{mission_id}/report", response_class=PlainTextResponse)
    def report(mission_id: str):
        row = _row(mission_id)
        return _path(mission_id, row["version"], "report.md").read_text()

    @app.get("/api/v1/missions/{mission_id}/ai-report")
    def automated_report(mission_id: str, background_tasks: BackgroundTasks):
        row = _row(mission_id)
        report_path = _path(mission_id, row["version"], "ai-report.json")
        if not report_path.is_file():
            schedule_report(background_tasks, mission_id, row["version"])
            return {
                "mission_id": mission_id,
                "snapshot_version": row["version"],
                "status": "pending",
                "report": None,
                "generated_by": None,
                "human_review_required": True,
            }
        return json.loads(report_path.read_text(encoding="utf-8"))

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
                "find_hazard_relevant_features",
            ],
        }

    @app.post("/api/v1/missions/{mission_id}/ask")
    def ask_mission(mission_id: str, request: MissionQuestion):
        from .agent import answer_question

        row = _row(mission_id)
        deterministic = _path(mission_id, row["version"], "report.md").read_text()
        return answer_question(store, mission_id, row, deterministic, request.question)

    web = Path(os.environ.get("WEB_DIR", REPO / "web"))
    if (web / "dist").is_dir():
        web = web / "dist"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


app = create_app() if os.environ.get("RESCUE_API_NO_AUTOCREATE") != "1" else None
