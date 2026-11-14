#!/usr/bin/env python3
"""Scripted outage rehearsal (the judging demo, automated).

Starts the cloud API (:8000) and the edge pipeline + local dashboard (:8080) in mock mode,
then: link up -> cut link -> versions pile up locally -> corrupt one upload -> restore
link -> cloud catches up to the newest version. Prints a timeline.

    python scripts/make_demo_data.py
    python scripts/demo_outage.py            # add --keep to leave both servers running
Open http://localhost:8080 (edge) and http://localhost:8000 (cloud) side by side.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
EDGE, CLOUD = "http://127.0.0.1:8080", "http://127.0.0.1:8000"


def wait_for(url: str, timeout: float = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            requests.get(url, timeout=1)
            return
        except requests.ConnectionError:
            time.sleep(0.3)
    raise SystemExit(f"{url} did not come up")


def edge_status() -> dict:
    return requests.get(f"{EDGE}/api/v1/status", timeout=2).json()


def cloud_version(mission: str) -> int:
    r = requests.get(f"{CLOUD}/api/v1/missions/{mission}/latest", timeout=2)
    return r.json()["manifest"]["version"] if r.ok else 0


def cloud_summary(mission: str) -> dict:
    r = requests.get(f"{CLOUD}/api/v1/missions/{mission}/latest", timeout=2)
    return r.json()["manifest"]["summary"] if r.ok else {}


def log(msg: str) -> None:
    st = edge_status()
    u = st["uploader"]
    print(f"[{time.strftime('%H:%M:%S')}] {msg:<44} edge v{u['local_version']:<3} "
          f"acked v{u['acked_version']:<3} cloud v{cloud_version(st['mission_id']):<3} "
          f"online={u['online']} frames={st['frames']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="leave servers running after the script")
    ap.add_argument("--fresh", action="store_true", help="delete previous edge/cloud data first")
    args = ap.parse_args()
    if args.fresh:
        shutil.rmtree(REPO / "data/edge", ignore_errors=True)
        shutil.rmtree(REPO / "data/cloud", ignore_errors=True)
    if not (REPO / "data/demo/detections.jsonl").exists():
        subprocess.run([sys.executable, str(REPO / "scripts/make_demo_data.py")], cwd=REPO, check=True)
    env = {**os.environ, "INGEST_API_KEY": os.environ.get("INGEST_API_KEY", "dev-key"),
           "PYTHONPATH": f"{REPO / 'edge'}{os.pathsep}{REPO / 'server'}"}
    flag = REPO / "data/edge/OFFLINE"
    flag.unlink(missing_ok=True)
    procs = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "rescue_api.app:app", "--port", "8000",
                          "--log-level", "warning"], cwd=REPO / "server", env=env),
    ]
    wait_for(f"{CLOUD}/api/v1/health")
    mission = f"demo-{time.strftime('%H%M%S')}"
    exit_code = 1
    procs.append(subprocess.Popen([sys.executable, "-m", "rescue_edge.main", "--config",
                                   str(REPO / "config/edge.mock.yaml"), "--serve", "--mission-id", mission],
                                  cwd=REPO / "edge", env=env, stderr=subprocess.DEVNULL))
    try:
        wait_for(f"{EDGE}/api/v1/status")
        print(f"mission {mission}\n  edge dashboard : {EDGE}\n  cloud dashboard: {CLOUD}\n")
        time.sleep(8)
        log("link up, snapshots flowing")
        requests.post(f"{EDGE}/api/v1/network", json={"offline": True})
        log("LINK CUT")
        for _ in range(3):
            time.sleep(5)
            log("offline: still detecting, versions pile up")
        requests.post(f"{EDGE}/api/v1/demo/corrupt-next-upload")
        requests.post(f"{EDGE}/api/v1/network", json={"offline": False})
        log("LINK RESTORED (first upload corrupted)")
        for _ in range(6):
            time.sleep(1.5)
            st = edge_status()
            if not st["uploader"]["pending"]:
                break
        log("caught up")
        while edge_status()["running"]:
            time.sleep(2)
        log("flight finished, waiting for final snapshot sync")
        deadline = time.monotonic() + 30
        st = edge_status()
        cloud = cloud_version(mission)
        while time.monotonic() < deadline:
            st = edge_status()
            cloud = cloud_version(mission)
            if not st["uploader"]["pending"] and cloud == st["uploader"]["local_version"]:
                break
            time.sleep(0.5)
        synced = cloud == st["uploader"]["local_version"] and not st["uploader"]["pending"]
        # A sync of an empty map is not a working demo: check that people were located.
        summary = cloud_summary(mission)
        located = summary.get("estimated_people", 0)
        failures = []
        if not synced:
            failures.append(f"local v{st['uploader']['local_version']}, cloud v{cloud}, "
                            f"pending={st['uploader']['pending']}")
        if located == 0:
            failures.append(f"no located people in the final snapshot "
                            f"(unlocated: {summary.get('unlocated_tracks')}, "
                            f"location failures: {summary.get('location_failures')})")
        result = (f"PASS: cloud has the newest snapshot with {located} estimated people"
                  if not failures else "FAIL: " + "; ".join(failures))
        print(f"\nRESULT: {result}")
        exit_code = 1 if failures else 0
        if args.keep:
            print("servers still running; Ctrl+C to stop")
            procs[1].wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
