CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS normalized;
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.ingestion_runs (
    collection_id text PRIMARY KEY,
    collected_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    completed_at timestamptz,
    error text
);
CREATE TABLE IF NOT EXISTS ops.ingestion_attempts (
    attempt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id text NOT NULL REFERENCES ops.ingestion_runs,
    operation text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    error text
);
CREATE TABLE IF NOT EXISTS raw.feed_payloads (
    raw_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id text NOT NULL REFERENCES ops.ingestion_runs,
    feed_name text NOT NULL,
    feed_url text NOT NULL,
    fetched_at timestamptz NOT NULL,
    source_published_at timestamptz,
    payload_hash text NOT NULL CHECK (length(payload_hash) = 64),
    payload jsonb NOT NULL,
    UNIQUE (collection_id, feed_name)
);
CREATE TABLE IF NOT EXISTS normalized.station_snapshots (
    collection_id text NOT NULL REFERENCES ops.ingestion_runs,
    station_id text NOT NULL,
    name text NOT NULL,
    lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
    capacity integer CHECK (capacity >= 0),
    information_raw_id bigint NOT NULL REFERENCES raw.feed_payloads,
    PRIMARY KEY (collection_id, station_id)
);
CREATE TABLE IF NOT EXISTS normalized.observations (
    collection_id text NOT NULL,
    station_id text NOT NULL,
    collected_at timestamptz NOT NULL,
    status_fetched_at timestamptz NOT NULL,
    source_published_at timestamptz NOT NULL,
    station_reported_at timestamptz NOT NULL,
    num_bikes_available integer NOT NULL CHECK (num_bikes_available >= 0),
    num_docks_available integer NOT NULL CHECK (num_docks_available >= 0),
    num_bikes_disabled integer CHECK (num_bikes_disabled >= 0),
    num_docks_disabled integer CHECK (num_docks_disabled >= 0),
    is_installed boolean NOT NULL,
    is_renting boolean NOT NULL,
    is_returning boolean NOT NULL,
    status_raw_id bigint NOT NULL REFERENCES raw.feed_payloads,
    PRIMARY KEY (collection_id, station_id),
    FOREIGN KEY (collection_id, station_id)
      REFERENCES normalized.station_snapshots (collection_id, station_id)
);
CREATE INDEX IF NOT EXISTS observations_station_time
  ON normalized.observations (station_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS runs_time ON ops.ingestion_runs (collected_at DESC);
CREATE TABLE IF NOT EXISTS ops.quality_checks (
    collection_id text NOT NULL REFERENCES ops.ingestion_runs,
    check_name text NOT NULL,
    passed boolean NOT NULL,
    details jsonb NOT NULL,
    PRIMARY KEY (collection_id, check_name)
);
CREATE TABLE IF NOT EXISTS ops.transformation_runs (
    transformation_id uuid PRIMARY KEY,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    dbt_results jsonb,
    error text
);
CREATE TABLE IF NOT EXISTS ops.source_backoff (
    discovery_url text PRIMARY KEY,
    retry_not_before timestamptz NOT NULL
);
