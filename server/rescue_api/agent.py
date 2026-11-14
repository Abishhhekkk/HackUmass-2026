"""Optional Gemini (via LangChain) report writer with allow-listed, read-only tools.

Safety boundaries:
* The model only sees aggregated mission facts, never imagery.
* Every tool is read-only and bound to ONE mission chosen by the server, not by the model.
* Arguments are validated and clamped (radius, result counts); results carry source/freshness.
* Landmarks come from OpenStreetMap (Overpass API), so the model doesn't invent them.
* Output is labelled "human review required"; the model cannot assign rescues or touch the drone.
* No key / no network / any error -> the deterministic report is returned instead.

Pinned: langchain-core==1.8.0, langchain-google-genai==4.5.0 (see server/requirements-ai.txt).
"""
from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timezone

import requests

OVERPASS_URL = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
MAX_STEPS = 6

SYSTEM_PROMPT = """You write short situation reports for search-and-rescue coordinators.
Use ONLY facts returned by the tools. Every location is an ESTIMATE from drone camera geometry
with the stated horizontal error; every count is "estimated people", never confirmed people.
Describe where estimated people cluster, relative to named landmarks from find_nearby_landmarks
(give the landmark name, its distance, and say the data is from OpenStreetMap). Mention data gaps
(unlocated sightings, location failures) and the data time window. Do NOT assign rescue teams,
rank who to rescue first, or give instructions to the drone. End with the line:
"Human review required - estimates only."
Format: Markdown, under 250 words."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def fetch_landmarks(lat: float, lon: float, radius_m: float, limit: int = 15) -> dict:
    q = f"""[out:json][timeout:15];
(
  node(around:{radius_m:.0f},{lat:.6f},{lon:.6f})[name][~"^(amenity|building|tourism|leisure|place|highway|emergency)$"~"."];
  way(around:{radius_m:.0f},{lat:.6f},{lon:.6f})[name][~"^(amenity|building|leisure|highway)$"~"."];
);
out center {limit * 3};"""
    r = requests.post(OVERPASS_URL, data={"data": q}, timeout=20,
                      headers={"User-Agent": "RescueSight-HackUMass/0.1"})
    r.raise_for_status()
    from .storage import _haversine

    out = []
    for el in r.json().get("elements", []):
        la = el.get("lat") or el.get("center", {}).get("lat")
        lo = el.get("lon") or el.get("center", {}).get("lon")
        if la is None:
            continue
        tags = el.get("tags", {})
        kind = next((f"{k}={tags[k]}" for k in ("amenity", "building", "tourism", "leisure", "place",
                                                  "highway", "emergency") if k in tags), "")
        out.append({"name": tags["name"], "kind": kind, "lat": la, "lon": lo,
                    "distance_m": round(_haversine(lat, lon, la, lo))})
    out.sort(key=lambda x: x["distance_m"])
    return {"source": "OpenStreetMap contributors via Overpass API", "fetched_at": _now(),
            "landmarks": out[:limit]}


def make_tools(store, mission_id: str, row: dict):
    from langchain_core.tools import tool

    heat_path = store.snapshot_dir / mission_id / f"v{row['version']:06d}" / "heatmap.geojson"

    @tool
    def get_mission_summary() -> dict:
        """Summary of the current mission snapshot: estimated people, unlocated sightings,
        location error statistics, data time window, grid size."""
        m = row["manifest"]
        return {"mission_id": mission_id, "snapshot_version": row["version"],
                "received_at": row["received_at"], "data_window": m.get("data_window"),
                "cell_size_m": m.get("cell_size_m"), "summary": m["summary"],
                "position_method": m.get("position_method")}

    @tool
    def get_densest_cells(limit: int = 5) -> list[dict]:
        """Grid cells with the most estimated people, with each cell's centre lat/lon."""
        n = int(_clamp(limit, 1, 10))
        heat = json.loads(heat_path.read_text())
        cells = [f for f in heat["features"] if f["properties"]["kind"] == "cell"]
        cells.sort(key=lambda f: (-f["properties"]["count"], -f["properties"]["score"]))
        out = []
        for f in cells[:n]:
            ring = f["geometry"]["coordinates"][0][:4]
            out.append({"cell_id": f["properties"]["cell_id"], "estimated_people": f["properties"]["count"],
                        "score": f["properties"]["score"],
                        "centre_lat": round(statistics.fmean(p[1] for p in ring), 6),
                        "centre_lon": round(statistics.fmean(p[0] for p in ring), 6)})
        return out

    @tool
    def get_people_near(lat: float, lon: float, radius_m: float = 50) -> list[dict]:
        """Estimated people within radius_m (max 500) of a point in this mission."""
        return store.people_near(mission_id, _clamp(lat, -90, 90), _clamp(lon, -180, 180),
                                 _clamp(radius_m, 1, 500), limit=50)

    @tool
    def find_nearby_landmarks(lat: float, lon: float, radius_m: float = 300) -> dict:
        """Named landmarks (buildings, roads, amenities) from OpenStreetMap near a point.
        radius_m max 1000."""
        try:
            return fetch_landmarks(_clamp(lat, -90, 90), _clamp(lon, -180, 180), _clamp(radius_m, 50, 1000))
        except Exception as e:
            return {"error": f"landmark lookup failed: {type(e).__name__}", "landmarks": []}

    return [get_mission_summary, get_densest_cells, get_people_near, find_nearby_landmarks]


def generate_report(store, mission_id: str, row: dict, deterministic: str, llm=None) -> dict:
    base = {"mission_id": mission_id, "snapshot_version": row["version"],
            "human_review_required": True}
    if llm is None:
        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            return {**base, "generated_by": "deterministic", "fallback_reason": "no GEMINI_API_KEY",
                    "report": deterministic}
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI

            llm = ChatGoogleGenerativeAI(model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                                         google_api_key=key, temperature=0.2, max_retries=1, timeout=30)
        except Exception as e:
            return {**base, "generated_by": "deterministic", "fallback_reason": f"LLM init failed: {e}",
                    "report": deterministic}
    try:
        from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

        tools = make_tools(store, mission_id, row)
        by_name = {t.name: t for t in tools}
        bound = llm.bind_tools(tools)
        msgs = [SystemMessage(SYSTEM_PROMPT),
                HumanMessage("Write the situation report for this mission. Start by calling "
                             "get_mission_summary and get_densest_cells, then look up landmarks "
                             "near the densest cells.")]
        calls_log = []
        for _ in range(MAX_STEPS):
            ai = bound.invoke(msgs)
            msgs.append(ai)
            if not getattr(ai, "tool_calls", None):
                text = ai.content if isinstance(ai.content, str) else "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in ai.content)
                if "Human review required" not in text:
                    text += "\n\nHuman review required - estimates only."
                return {**base, "generated_by": "gemini", "report": text, "tool_calls": calls_log,
                        "deterministic_report": deterministic}
            for call in ai.tool_calls:
                t = by_name.get(call["name"])
                if t is None:
                    result = {"error": f"tool {call['name']} is not allowed"}
                else:
                    try:
                        result = t.invoke(call.get("args", {}))
                    except Exception as e:  # bad arguments etc.
                        result = {"error": f"{type(e).__name__}: {e}"[:200]}
                calls_log.append({"tool": call["name"], "args": call.get("args", {})})
                msgs.append(ToolMessage(json.dumps(result, default=str)[:8000], tool_call_id=call["id"]))
        raise RuntimeError("tool loop did not finish")
    except Exception as e:
        return {**base, "generated_by": "deterministic", "fallback_reason": f"{type(e).__name__}: {e}"[:300],
                "report": deterministic}
