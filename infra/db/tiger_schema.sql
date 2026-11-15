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

-- Aggregated vision heatmap cells from each complete mission snapshot.
CREATE TABLE IF NOT EXISTS heatmap_cells (
  mission_id   TEXT NOT NULL,
  cell_id      TEXT NOT NULL,
  version      INTEGER NOT NULL,
  count        INTEGER NOT NULL,
  score        DOUBLE PRECISION NOT NULL,
  person_ids   TEXT NOT NULL,
  geometry_json TEXT NOT NULL,
  geom geography(Polygon, 4326) NOT NULL,
  PRIMARY KEY (mission_id, cell_id)
);
CREATE INDEX IF NOT EXISTS heatmap_cells_geom_idx ON heatmap_cells USING GIST (geom);

CREATE TABLE IF NOT EXISTS unlocated_sightings (
  mission_id TEXT NOT NULL,
  person_id  TEXT NOT NULL,
  version    INTEGER NOT NULL,
  confidence DOUBLE PRECISION,
  first_seen TEXT,
  last_seen  TEXT,
  reason     TEXT NOT NULL,
  track_ids  TEXT,
  PRIMARY KEY (mission_id, person_id)
);

-- Drone pose history from each snapshot's flight_path.csv (~1 Hz, frame-aligned).
-- Rows with valid = false are telemetry gaps (lat/lon NULL, reason set), never guessed fixes.
CREATE TABLE IF NOT EXISTS drone_telemetry (
  mission_id  TEXT NOT NULL,
  drone_id    TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL,      -- GPS-clock time of the frame the pose is aligned to
  t_epoch     DOUBLE PRECISION NOT NULL, -- same instant as epoch seconds (used for incremental insert)
  lat DOUBLE PRECISION,
  lon DOUBLE PRECISION,
  alt_agl_m   DOUBLE PRECISION,
  heading_deg DOUBLE PRECISION,
  pitch_deg   DOUBLE PRECISION,
  roll_deg    DOUBLE PRECISION,
  valid       BOOLEAN NOT NULL,
  reason      TEXT,
  geom geography(Point, 4326) GENERATED ALWAYS AS
       (CASE WHEN lat IS NULL OR lon IS NULL THEN NULL
             ELSE ST_SetSRID(ST_MakePoint(lon, lat), 4326)::geography END) STORED,
  PRIMARY KEY (mission_id, drone_id, observed_at)   -- includes the time column, as hypertables require
);

-- Snapshot files (csv/geojson/report/telemetry/manifest), stored here so the API keeps nothing on
-- its own disk. Contents are kept for the newest few versions per mission (see storage.py).
CREATE TABLE IF NOT EXISTS snapshot_files (
  mission_id TEXT NOT NULL,
  version    INTEGER NOT NULL,
  name       TEXT NOT NULL,
  sha256     TEXT NOT NULL,
  content    BYTEA NOT NULL,
  PRIMARY KEY (mission_id, version, name)
);

-- Mission reports (Gemini or deterministic fallback), one per snapshot version reported on.
CREATE TABLE IF NOT EXISTS ai_reports (
  mission_id   TEXT NOT NULL,
  version      INTEGER NOT NULL,
  generated_at TEXT NOT NULL,
  payload      TEXT NOT NULL,           -- the full report JSON
  PRIMARY KEY (mission_id, version)
);

-- On Tiger Data / TimescaleDB, make it a hypertable (time-partitioned). Plain Postgres skips this.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    PERFORM create_hypertable('drone_telemetry', 'observed_at',
                              if_not_exists => TRUE, migrate_data => TRUE);
  END IF;
END
$$;
