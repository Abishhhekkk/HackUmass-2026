"""Local dashboard + read API served from the Jetson (works with zero internet).

Exposes the same read routes as the cloud API, so web/ runs unchanged against either.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .snapshot import latest_version, snapshot_dir

NOT_BUILT_HTML = """<!doctype html><meta charset="utf-8"><title>Vantage: dashboard not built</title>
<body style="font-family:system-ui,sans-serif;max-width:40rem;margin:3rem auto;padding:0 1rem">
<h1>Dashboard not built</h1>
<p>The API is running, but <code>web/dist</code> does not exist, so there is no dashboard to serve.
Build it once from the repository root:</p>
<pre>cd web
npm ci
npm run build</pre>
<p>Then reload this page (no restart needed). The API itself works now, e.g.
<a href="/api/v1/status">/api/v1/status</a>.</p></body>"""

ALLOWED_FILES = {"sightings.csv", "heatmap.geojson", "report.md", "telemetry.json", "manifest.json",
                 "flight_path.csv"}


class NetworkToggle(BaseModel):
    offline: bool


def create_app(pipe, web_dir: Path) -> FastAPI:
    app = FastAPI(title="Vantage edge")
    root: Path = pipe.root

    def _dir(mission_id: str, version: int | None) -> tuple[Path, int]:
        if not re.match(r"^[A-Za-z0-9._-]{1,80}$", mission_id) or mission_id in {".", ".."}:
            raise HTTPException(404)
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
            pipe.uploader.online = False
        else:
            flag.unlink(missing_ok=True)
            pipe.uploader.online = None
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
        file_path = d / name
        if not file_path.is_file():
            raise HTTPException(404, f"{name} is not present in this snapshot")
        return FileResponse(file_path, filename=f"{mission_id}-{name}")

    @app.get("/api/v1/missions/{mission_id}/flight-path")
    def flight_path(mission_id: str):
        """Same response as the cloud route, read from the latest snapshot (no database here)."""
        d, _ = _dir(mission_id, None)
        path = d / "flight_path.csv"
        rows = []
        if path.is_file():
            for r in csv.DictReader(path.open(newline="")):
                valid = r["valid"] == "1"
                rows.append({"observed_at": r["observed_at"], "valid": valid,
                             "lat": float(r["lat"]) if valid else None,
                             "lon": float(r["lon"]) if valid else None})
        return flight_path_geojson(rows)

    @app.get("/api/v1/missions/{mission_id}/report", response_class=PlainTextResponse)
    def report(mission_id: str):
        d, _ = _dir(mission_id, None)
        return (d / "report.md").read_text()

    mount_dashboard(app, web_dir / "dist")
    return app


def mount_dashboard(app: FastAPI, dist: Path) -> None:
    """Serve the built React dashboard. web/index.html itself loads /src/main.jsx, which a
    browser cannot run, so without a build we show how to build instead of a blank page.
    The check happens per request, so building while the server runs is enough."""
    static = StaticFiles(directory=dist, html=True, check_dir=False)

    async def dashboard(scope, receive, send):
        if (dist / "index.html").is_file():
            await static(scope, receive, send)
        else:
            await HTMLResponse(NOT_BUILT_HTML, status_code=503)(scope, receive, send)

    app.mount("/", dashboard, name="web")


def flight_path_geojson(rows: list[dict]) -> dict:
    """Mirror of rescue_api.app.flight_path_geojson (the cloud API is deployed without this
    package). Valid fixes become LineStrings; a telemetry gap ends a segment."""
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
