-- TigerData (Postgres) schema. Applied automatically by the API at startup (idempotent).
-- Requires PostGIS: on Tiger Cloud enable it once with  CREATE EXTENSION postgis;
-- (the statement below does it if your role is allowed to).
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS snapshots (
  mission_id   TEXT NOT NULL,
  version      INTEGER NOT NULL,
  drone_id     TEXT NOT NULL,
  created_at   TEXT NOT NULL,           -- when the Jetson built the snapshot (GPS clock, ISO-8601 UTC)
  received_at  TEXT NOT NULL,           -- when the cloud accepted it
  content_hash TEXT NOT NULL,
  manifest     TEXT NOT NULL,           -- full manifest JSON (cast to jsonb for queries)
  PRIMARY KEY (mission_id, version)
);

-- Mirrors the newest snapshot of each mission (snapshots are full state, not deltas).
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
  geom geography(Point, 4326) GENERATED ALWAYS AS
       (ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography) STORED,
  PRIMARY KEY (mission_id, person_id)
);
CREATE INDEX IF NOT EXISTS people_geom_idx ON people USING GIST (geom);
