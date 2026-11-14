"""Snapshot index + people table. SQLite by default; Postgres/TigerData when DATABASE_URL is set.

Files (csv/geojson/report) live on disk under SNAPSHOT_DIR in the same layout the edge
uses. On Vultr, mount block storage there, or swap in Vultr Object Storage (S3 API).
"""
from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  mission_id   TEXT NOT NULL,
  version      INTEGER NOT NULL,
  drone_id     TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  received_at  TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  manifest     TEXT NOT NULL,
  PRIMARY KEY (mission_id, version)
);
CREATE TABLE IF NOT EXISTS people (
  mission_id TEXT NOT NULL,
  person_id  TEXT NOT NULL,
  version    INTEGER NOT NULL,
  lat DOUBLE PRECISION NOT NULL,
  lon DOUBLE PRECISION NOT NULL,
  horizontal_error_m DOUBLE PRECISION,
  confidence DOUBLE PRECISION,
  first_seen TEXT,
  last_seen  TEXT,
  cell_id    TEXT,
  PRIMARY KEY (mission_id, person_id)
);
CREATE TABLE IF NOT EXISTS heatmap_cells (
  mission_id TEXT NOT NULL,
  cell_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  count INTEGER NOT NULL,
  score DOUBLE PRECISION NOT NULL,
  person_ids TEXT NOT NULL,
  geometry_json TEXT NOT NULL,
  PRIMARY KEY (mission_id, cell_id)
);
CREATE TABLE IF NOT EXISTS unlocated_sightings (
  mission_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  confidence DOUBLE PRECISION,
  first_seen TEXT,
  last_seen TEXT,
  reason TEXT NOT NULL,
  track_ids TEXT,
  PRIMARY KEY (mission_id, person_id)
);
CREATE TABLE IF NOT EXISTS drone_telemetry (
  mission_id  TEXT NOT NULL,
  drone_id    TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  t_epoch     DOUBLE PRECISION NOT NULL,
  lat DOUBLE PRECISION,
  lon DOUBLE PRECISION,
  alt_agl_m   DOUBLE PRECISION,
  heading_deg DOUBLE PRECISION,
  pitch_deg   DOUBLE PRECISION,
  roll_deg    DOUBLE PRECISION,
  valid       BOOLEAN NOT NULL,
  reason      TEXT,
  PRIMARY KEY (mission_id, drone_id, observed_at)
);
"""

PG_SCHEMA_FILE = Path(__file__).resolve().parents[2] / "infra" / "db" / "tiger_schema.sql"


class Storage:
    def __init__(self, database_url: str | None, snapshot_dir: str | Path):
        self.snapshot_dir = Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.pg = bool(database_url and database_url.startswith(("postgres://", "postgresql://")))
        self.ph = "%s" if self.pg else "?"
        self._lock = threading.Lock()
        if self.pg:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError(
                    "DATABASE_URL points at Postgres/Tiger Data but the driver is not installed. "
                    "Run: pip install -r server/requirements-postgres.txt "
                    "(or unset DATABASE_URL to use SQLite)."
                ) from exc

            self._conn = psycopg.connect(database_url, autocommit=False)
            with self._conn.cursor() as cur:
                cur.execute(PG_SCHEMA_FILE.read_text())
            self._conn.commit()
        else:
            path = (database_url or "sqlite:///data/cloud/rescue.db").removeprefix("sqlite:///")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.executescript(SQLITE_SCHEMA)
            self._conn.commit()

    @contextmanager
    def tx(self):
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    def _q(self, sql: str) -> str:
        return sql.replace("?", self.ph)

    # ------------------------------------------------------------------ writes
    def latest(self, cur, mission_id: str) -> tuple[int, str | None]:
        cur.execute(self._q("SELECT version, content_hash FROM snapshots WHERE mission_id=? "
                            "ORDER BY version DESC LIMIT 1"), (mission_id,))
        row = cur.fetchone()
        return (row[0], row[1]) if row else (0, None)

    def insert_snapshot(
        self, cur, manifest: dict, received_at: str, sightings_csv: bytes, heatmap_geojson: bytes
    ) -> None:
        cur.execute(self._q(
            "INSERT INTO snapshots (mission_id, version, drone_id, created_at, received_at, content_hash, manifest) "
            "VALUES (?,?,?,?,?,?,?)"),
            (manifest["mission_id"], manifest["version"], manifest["drone_id"], manifest["created_at"],
             received_at, manifest["content_hash"], json.dumps(manifest)))
        # people table always mirrors the newest snapshot (it is a full state, not a delta)
        cur.execute(self._q("DELETE FROM people WHERE mission_id=?"), (manifest["mission_id"],))
        cur.execute(self._q("DELETE FROM heatmap_cells WHERE mission_id=?"), (manifest["mission_id"],))
        cur.execute(self._q("DELETE FROM unlocated_sightings WHERE mission_id=?"), (manifest["mission_id"],))
        rows = []
        unlocated = []
        for r in csv.DictReader(io.StringIO(sightings_csv.decode())):
            if r["status"] == "unlocated_sighting":
                unlocated.append((
                    manifest["mission_id"], r["person_id"], manifest["version"],
                    float(r["confidence"]) if r["confidence"] else None,
                    r["first_seen_utc"] or None, r["last_seen_utc"] or None,
                    r["unlocated_reason"], r["track_ids"],
                ))
                continue
            if r["status"] != "estimated_sighting":
                continue
            rows.append((manifest["mission_id"], r["person_id"], manifest["version"], float(r["lat"]),
                         float(r["lon"]), float(r["horizontal_error_m"]), float(r["confidence"]),
                         r["first_seen_utc"], r["last_seen_utc"], r["cell_id"]))
        if rows:
            cur.executemany(self._q(
                "INSERT INTO people (mission_id, person_id, version, lat, lon, horizontal_error_m, "
                "confidence, first_seen, last_seen, cell_id) VALUES (?,?,?,?,?,?,?,?,?,?)"), rows)
        if unlocated:
            cur.executemany(self._q(
                "INSERT INTO unlocated_sightings "
                "(mission_id, person_id, version, confidence, first_seen, last_seen, reason, track_ids) "
                "VALUES (?,?,?,?,?,?,?,?)"), unlocated)
        heatmap = json.loads(heatmap_geojson)
        cells = []
        for feature in heatmap.get("features", []):
            props = feature.get("properties", {})
            if props.get("kind") != "cell" or feature.get("geometry", {}).get("type") != "Polygon":
                continue
            geometry = feature["geometry"]
            geometry_json = json.dumps(geometry, separators=(",", ":"))
            person_ids = json.dumps(props.get("person_ids", []), separators=(",", ":"))
            values = (
                manifest["mission_id"], props["cell_id"], manifest["version"],
                int(props["count"]), float(props["score"]), person_ids, geometry_json,
            )
            if self.pg:
                cur.execute(
                    "INSERT INTO heatmap_cells "
                    "(mission_id, cell_id, version, count, score, person_ids, geometry_json, geom) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,"
                    "ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)::geography)",
                    (*values, geometry_json),
                )
            else:
                cells.append(values)
        if cells:
            cur.executemany(self._q(
                "INSERT INTO heatmap_cells "
                "(mission_id, cell_id, version, count, score, person_ids, geometry_json) "
                "VALUES (?,?,?,?,?,?,?)"), cells)

    def insert_flight_path(self, cur, manifest: dict, flight_path_csv: bytes | None) -> int:
        """Append the snapshot's drone track to drone_telemetry (a Timescale hypertable on
        Tiger Data). Snapshots carry the whole track, so only rows newer than what is stored
        are inserted; ON CONFLICT makes a re-send harmless. Returns rows inserted."""
        if not flight_path_csv:
            return 0
        mission_id, drone_id = manifest["mission_id"], manifest["drone_id"]
        cur.execute(self._q("SELECT MAX(t_epoch) FROM drone_telemetry WHERE mission_id=? AND drone_id=?"),
                    (mission_id, drone_id))
        latest = cur.fetchone()[0]

        def num(v: str):
            return float(v) if v not in ("", None) else None

        rows = []
        for r in csv.DictReader(io.StringIO(flight_path_csv.decode())):
            t = float(r["t"])
            if latest is not None and t <= latest:
                continue
            valid = r["valid"] in ("1", "true", "True")
            rows.append((mission_id, drone_id, r["observed_at"], t,
                         num(r["lat"]) if valid else None, num(r["lon"]) if valid else None,
                         num(r["alt_agl_m"]), num(r["heading_deg"]), num(r["pitch_deg"]),
                         num(r["roll_deg"]), valid, r["reason"] or None))
        if rows:
            ts = "?::timestamptz" if self.pg else "?"
            cur.executemany(self._q(
                "INSERT INTO drone_telemetry (mission_id, drone_id, observed_at, t_epoch, lat, lon, "
                "alt_agl_m, heading_deg, pitch_deg, roll_deg, valid, reason) "
                f"VALUES (?,?,{ts},?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING"), rows)
        return len(rows)

    # ------------------------------------------------------------------ reads
    def flight_path(self, mission_id: str, limit: int = 20000) -> list[dict]:
        """Drone track in time order (newest `limit` rows), from the time-series table."""
        with self.tx() as cur:
            cur.execute(self._q(
                "SELECT drone_id, observed_at, t_epoch, lat, lon, alt_agl_m, heading_deg, valid, reason "
                "FROM drone_telemetry WHERE mission_id=? ORDER BY t_epoch DESC LIMIT ?"),
                (mission_id, limit))
            rows = cur.fetchall()
        out = []
        for drone, observed, t, lat, lon, alt, hdg, valid, reason in reversed(rows):
            if not isinstance(observed, str):  # Postgres returns datetime
                observed = observed.isoformat().replace("+00:00", "Z")
            out.append({"drone_id": drone, "observed_at": observed, "t": t, "lat": lat, "lon": lon,
                        "alt_agl_m": alt, "heading_deg": hdg, "valid": bool(valid), "reason": reason})
        return out

    def missions(self) -> list[dict]:
        with self.tx() as cur:
            cur.execute(
                "SELECT s.mission_id, s.version, s.drone_id, s.created_at, s.received_at, s.manifest "
                "FROM snapshots s JOIN (SELECT mission_id, MAX(version) AS v FROM snapshots "
                "GROUP BY mission_id) m ON s.mission_id = m.mission_id AND s.version = m.v "
                "ORDER BY s.received_at DESC")
            out = []
            for mid, v, drone, created, received, man in cur.fetchall():
                out.append({"mission_id": mid, "drone_id": drone, "latest_version": v,
                            "created_at": created, "received_at": received,
                            "summary": json.loads(man)["summary"]})
            return out

    def snapshot_row(self, mission_id: str, version: int | None = None) -> dict | None:
        with self.tx() as cur:
            if version is None:
                cur.execute(self._q("SELECT version, received_at, manifest FROM snapshots WHERE mission_id=? "
                                    "ORDER BY version DESC LIMIT 1"), (mission_id,))
            else:
                cur.execute(self._q("SELECT version, received_at, manifest FROM snapshots "
                                    "WHERE mission_id=? AND version=?"), (mission_id, version))
            row = cur.fetchone()
        if not row:
            return None
        return {"version": row[0], "received_at": row[1], "manifest": json.loads(row[2])}

    def people_near(self, mission_id: str, lat: float, lon: float, radius_m: float, limit: int = 50) -> list[dict]:
        with self.tx() as cur:
            if self.pg:  # PostGIS spatial query on the generated geography column
                cur.execute(
                    "SELECT person_id, lat, lon, horizontal_error_m, confidence, last_seen, "
                    "ST_Distance(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) AS d "
                    "FROM people WHERE mission_id=%s AND "
                    "ST_DWithin(geom, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, %s) "
                    "ORDER BY d LIMIT %s", (lon, lat, mission_id, lon, lat, radius_m, limit))
                rows = cur.fetchall()
            else:
                cur.execute("SELECT person_id, lat, lon, horizontal_error_m, confidence, last_seen "
                            "FROM people WHERE mission_id=?", (mission_id,))
                rows = [(*r, _haversine(lat, lon, r[1], r[2])) for r in cur.fetchall()]
                rows = sorted((r for r in rows if r[6] <= radius_m), key=lambda r: r[6])[:limit]
        return [{"person_id": r[0], "lat": r[1], "lon": r[2], "horizontal_error_m": r[3],
                 "confidence": r[4], "last_seen_utc": r[5], "distance_m": round(r[6], 1)} for r in rows]


def _haversine(lat1, lon1, lat2, lon2) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
