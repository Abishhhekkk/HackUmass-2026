#!/usr/bin/env python3
"""End-to-end check: Jetson-style upload -> cloud API (e.g. via Cloudflare Tunnel) -> database.

    python scripts/check_cloud.py --url https://<name>.trycloudflare.com
    python scripts/check_cloud.py --url http://127.0.0.1:8000 --database-url "$DATABASE_URL"

Builds a small synthetic mission ("check-<time>", 3 estimated people + a 10 s drone track) the
same way the Jetson does, uploads it with the Jetson's own uploader code, re-sends it (must be
'duplicate'), then reads it back through the API. With --database-url (or DATABASE_URL in the
environment/.env) it also queries the database directly (Tiger Data): rows in each table and
whether drone_telemetry is a TimescaleDB hypertable. Exit code 0 = every check passed.
The API key comes from --api-key or INGEST_API_KEY. --cleanup deletes the check mission's rows
afterwards (needs --database-url). Read-only otherwise, apart from the one check mission.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "edge"))
from rescue_edge.aggregate import Aggregator  # noqa: E402
from rescue_edge.geolocate import Pose, offset_to_latlon  # noqa: E402
from rescue_edge.snapshot import SnapshotWriter  # noqa: E402
from rescue_edge.telemetry import FlightPathRecorder, PoseLookup  # noqa: E402
from rescue_edge.uploader import Uploader  # noqa: E402

LAT, LON = 42.38925, -72.52830
TABLES = ("snapshots", "people", "heatmap_cells", "unlocated_sightings", "drone_telemetry",
          "snapshot_files", "ai_reports")
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    results.append((bool(ok), what))
    print(f"[{'PASS' if ok else 'FAIL'}] {what}", flush=True)
    return bool(ok)


def build_mission(root: Path, mission_id: str) -> int:
    """Write snapshot v1 like the Jetson does; returns how many files it holds (incl. manifest)."""
    t0 = time.time() - 60
    agg, rec = Aggregator(), FlightPathRecorder(1.0)
    for person in range(3):
        lat, lon = offset_to_latlon(LAT, LON, 15.0 * person, 5.0)
        for i in range(5):
            agg.add(t0 + person * 5 + i * 0.2, person + 1, 0.85, lat, lon, 3.0, None)
            agg.note_frame(t0 + person * 5 + i * 0.2)
    for i in range(10):
        lat, lon = offset_to_latlon(LAT, LON, 0.0, 2.0 * i)
        rec.record(t0 + i, PoseLookup(Pose(lat, lon, 30.0, 90.0, 0.0, 0.0)))
    SnapshotWriter(root, mission_id, "drone-check").write_if_changed(agg.finalize(), flight_path=rec.rows())
    return len(list((root / mission_id / "v000001").iterdir()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="API base URL, e.g. https://xyz.trycloudflare.com")
    ap.add_argument("--api-key", default=None, help="default: INGEST_API_KEY")
    ap.add_argument("--database-url", default=None, help="default: DATABASE_URL (optional)")
    ap.add_argument("--cleanup", action="store_true", help="delete the check mission's rows afterwards")
    args = ap.parse_args()
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO / ".env", override=False)
    except ImportError:
        pass
    api_key = args.api_key or os.environ.get("INGEST_API_KEY")
    db_url = args.database_url or os.environ.get("DATABASE_URL")
    base = args.url.rstrip("/")

    try:
        health = requests.get(f"{base}/api/v1/health", timeout=20).json()
    except Exception as e:  # noqa: BLE001
        check(False, f"reach {base}/api/v1/health ({type(e).__name__}: {e})")
        return 1
    check(health.get("ok") is True, f"API reachable at {base}")
    check(health.get("db") == "postgres", f"API uses Postgres/Tiger Data (reports db={health.get('db')!r})")

    mission = f"check-{time.strftime('%Y%m%d-%H%M%S')}"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        n_files = build_mission(root, mission)
        up = Uploader(root, mission, base, api_key=api_key)
        try:
            first = up.upload_once(1)
        except Exception as e:  # noqa: BLE001
            check(False, f"upload snapshot v1 with the Jetson's uploader ({e})")
            if "401" in str(e):
                print("  -> the API key differs: set INGEST_API_KEY to the server's value")
            return 1
        check(first.get("status") == "accepted", f"upload accepted (mission {mission})")
        check(up.upload_once(1).get("status") == "duplicate", "re-sent upload recognised as duplicate")

    latest = requests.get(f"{base}/api/v1/missions/{mission}/latest", timeout=20).json()
    check(latest.get("manifest", {}).get("summary", {}).get("estimated_people") == 3,
          "API returns the mission with 3 estimated people")
    fp = requests.get(f"{base}/api/v1/missions/{mission}/flight-path", timeout=20).json()
    check(fp.get("properties", {}).get("samples") == 10, "drone track (10 samples) readable via the API")
    csv_text = requests.get(f"{base}/api/v1/missions/{mission}/files/sightings.csv", timeout=20).text
    check(csv_text.startswith("person_id,"), "snapshot file served from the database")

    if not db_url:
        print("\n(no --database-url / DATABASE_URL: skipped the direct database checks)")
    else:
        try:
            import psycopg
        except ImportError:
            check(False, "pip install -r server/requirements-postgres.txt for the database checks")
        else:
            with psycopg.connect(db_url, connect_timeout=15) as conn:
                for table in TABLES:
                    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE mission_id = %s",
                                     (mission,)).fetchone()[0]
                    expect = {"snapshots": 1, "people": 3, "drone_telemetry": 10, "snapshot_files": n_files}
                    if table in expect:
                        check(n == expect[table], f"database: {table} has {n} row(s) (expected {expect[table]})")
                    else:
                        print(f"       database: {table} has {n} row(s)")
                ts = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'").fetchone()
                if ts:
                    hyper = conn.execute("SELECT 1 FROM timescaledb_information.hypertables "
                                         "WHERE hypertable_name = 'drone_telemetry'").fetchone()
                    check(hyper is not None, "drone_telemetry is a TimescaleDB hypertable")
                else:
                    print("       TimescaleDB not installed here: drone_telemetry is a plain table")
                if args.cleanup:
                    for table in TABLES:
                        conn.execute(f"DELETE FROM {table} WHERE mission_id = %s", (mission,))
                    print(f"       deleted the rows of {mission}")

    failed = [w for ok, w in results if not ok]
    print(f"\n{'ALL CHECKS PASSED' if not failed else f'{len(failed)} CHECK(S) FAILED'}: "
          f"{len(results) - len(failed)}/{len(results)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
