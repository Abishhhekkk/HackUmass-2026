#!/usr/bin/env python3
"""Save one finished mission into the website, so a static copy (Netlify) shows it to anyone.

    python scripts/export_demo.py                       # newest mission from the laptop's API
    python scripts/export_demo.py --mission mission-20261115-063640
    python scripts/export_demo.py --api http://127.0.0.1:8080/api/v1   # from the Jetson instead
    cd web && npm run build                             # then deploy web/dist

Writes web/public/demo/ (git-ignored: mission output, not code). The website uses it only when
no live service answers; on the Jetson and the laptop it keeps showing live data.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "public" / "demo"
# Asked once at export time through the cloud API (Gemini + mission tools); the saved answers
# let the static site answer these questions. Keep in step with QUESTIONS in web/src/Home.jsx.
QUESTIONS = [
    "Where is the densest cluster of sightings?",
    "Which sightings have the largest search radius?",
    "Summarise this mission for the incident commander.",
]
FILES = ["sightings.csv", "heatmap.geojson", "report.md", "telemetry.json", "manifest.json", "flight_path.csv"]


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"ngrok-skip-browser-warning": "1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def post_json(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "ngrok-skip-browser-warning": "1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000/api/v1")
    ap.add_argument("--mission", help="mission id (default: the newest)")
    ap.add_argument("--no-answers", action="store_true", help="skip asking the suggested questions")
    args = ap.parse_args()
    api = args.api.rstrip("/")

    try:
        status = json.loads(get(f"{api}/status"))
        missions = json.loads(get(f"{api}/missions"))
    except Exception as e:
        raise SystemExit(f"cannot reach {api} ({e}). Start Vantage-Cloud.bat (or the Jetson) first.")
    if not missions:
        raise SystemExit("no missions on that service yet")
    chosen = next((m for m in missions if m["mission_id"] == args.mission), None) if args.mission else missions[0]
    if not chosen:
        raise SystemExit(f"mission {args.mission!r} not found. Available: "
                         + ", ".join(m["mission_id"] for m in missions))
    mid = chosen["mission_id"]
    q = urllib.parse.quote(mid, safe="")

    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "files").mkdir(parents=True)
    latest = json.loads(get(f"{api}/missions/{q}/latest"))
    version = latest["manifest"]["version"]
    (OUT / "latest.json").write_text(json.dumps(latest))
    (OUT / "missions.json").write_text(json.dumps([chosen]))
    (OUT / "status.json").write_text(json.dumps(
        {"role": "demo", "mission_id": mid, "running": False, "source_role": status.get("role")}))
    try:
        (OUT / "flight-path.json").write_bytes(get(f"{api}/missions/{q}/flight-path"))
    except Exception:
        print("  (no flight path)")
    try:
        (OUT / "ai-report.json").write_bytes(get(f"{api}/missions/{q}/ai-report"))
    except Exception:
        print("  (no AI report: the service is not the cloud API, or none was written)")
    answers = []
    if status.get("role") == "cloud" and not args.no_answers:
        for question in QUESTIONS:
            print(f"  asking: {question}", flush=True)
            try:
                r = post_json(f"{api}/missions/{q}/ask", {"question": question})
            except Exception as e:
                print(f"    no answer ({e})")
                continue
            answers.append({"question": question, "answer": r.get("answer", ""),
                            "generated_by": r.get("generated_by"), "tool_calls": r.get("tool_calls", [])})
            print(f"    answered by {r.get('generated_by')}")
        (OUT / "answers.json").write_text(json.dumps(answers))
    elif not args.no_answers:
        print("  (questions not saved: answers come from the cloud API, use the laptop's address)")
    for name in FILES:
        try:
            (OUT / "files" / name).write_bytes(get(f"{api}/missions/{q}/files/{name}?version={version}"))
        except Exception:
            print(f"  (no {name})")

    s = chosen.get("summary", {})
    print(f"Saved {mid} v{version}: {s.get('estimated_people')} people placed, "
          f"{s.get('unlocated_tracks')} not placed -> {OUT}")
    print("Next: cd web && npm run build, then deploy web/dist (Netlify: drag it into Deploys).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
