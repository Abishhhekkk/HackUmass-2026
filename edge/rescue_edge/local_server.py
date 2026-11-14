"""Local dashboard + read API served from the Jetson (works with zero internet).

Exposes the same read routes as the cloud API, so web/ runs unchanged against either.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .snapshot import latest_version, snapshot_dir

ALLOWED_FILES = {"sightings.csv", "heatmap.geojson", "report.md", "manifest.json"}


class NetworkToggle(BaseModel):
    offline: bool


def create_app(pipe, web_dir: Path) -> FastAPI:
    app = FastAPI(title="RescueSight edge")
    root: Path = pipe.root

    def _dir(mission_id: str, version: int | None) -> tuple[Path, int]:
        v = version or latest_version(root / mission_id)
        d = snapshot_dir(root, mission_id, v)
        if not v or not (d / "manifest.json").exists():
            raise HTTPException(404, "no snapshot yet")
        return d, v

    @app.get("/api/v1/status")
    def status():
        return pipe.status()

    @app.post("/api/v1/network")
    def set_network(body: NetworkToggle):
        """Demo control: simulate losing / regaining the uplink."""
        if not pipe.uploader or not pipe.uploader.offline_flag:
            raise HTTPException(400, "uploader or offline_flag_file not configured")
        flag = pipe.uploader.offline_flag
        if body.offline:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.touch()
        else:
            flag.unlink(missing_ok=True)
            pipe.uploader.poke()
        return {"offline": body.offline}

    @app.post("/api/v1/demo/corrupt-next-upload")
    def corrupt_next():
        if not pipe.uploader:
            raise HTTPException(400, "uploader not configured")
        pipe.uploader.corrupt_next = True
        return {"ok": True}

    @app.get("/api/v1/missions")
    def missions():
        out = []
        for d in sorted(root.iterdir()) if root.exists() else []:
            v = latest_version(d) if d.is_dir() else 0
            if v:
                m = json.loads((snapshot_dir(root, d.name, v) / "manifest.json").read_text())
                out.append({"mission_id": d.name, "drone_id": m["drone_id"], "latest_version": v,
                            "created_at": m["created_at"], "summary": m["summary"]})
        return out

    @app.get("/api/v1/missions/{mission_id}/latest")
    def latest(mission_id: str):
        d, v = _dir(mission_id, None)
        return {"manifest": json.loads((d / "manifest.json").read_text()),
                "heatmap": json.loads((d / "heatmap.geojson").read_text()),
                "received_at": None, "source": "edge"}

    @app.get("/api/v1/missions/{mission_id}/files/{name}")
    def file(mission_id: str, name: str, version: int | None = None):
        if name not in ALLOWED_FILES:
            raise HTTPException(404)
        d, _ = _dir(mission_id, version)
        return FileResponse(d / name, filename=f"{mission_id}-{name}")

    @app.get("/api/v1/missions/{mission_id}/report", response_class=PlainTextResponse)
    def report(mission_id: str):
        d, _ = _dir(mission_id, None)
        return (d / "report.md").read_text()

    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
