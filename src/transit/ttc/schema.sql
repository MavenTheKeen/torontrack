-- TTC subway: static GTFS (versioned), realtime raw snapshots, normalized arrivals/alerts.

-- Static GTFS, subway routes only. Rows are immutable per feed_version (zip SHA-256).
CREATE TABLE IF NOT EXISTS normalized.ttc_gtfs_versions (
    feed_version text PRIMARY KEY CHECK (length(feed_version) = 64),
    source_url text NOT NULL,
    downloaded_at timestamptz NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    service_start date,
    service_end date,
    route_count integer NOT NULL,
    stop_count integer NOT NULL,
    trip_count integer NOT NULL,
    stop_time_count integer NOT NULL
);
CREATE TABLE IF NOT EXISTS normalized.ttc_routes (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    route_id text NOT NULL,
    short_name text NOT NULL,
    long_name text NOT NULL,
    color text,
    text_color text,
    PRIMARY KEY (feed_version, route_id)
);
CREATE TABLE IF NOT EXISTS normalized.ttc_stops (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    stop_id text NOT NULL,
    stop_name text NOT NULL,
    station_name text NOT NULL,
    station_key text NOT NULL,
    platform text NOT NULL,
    lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
    wheelchair_boarding smallint NOT NULL,
    PRIMARY KEY (feed_version, stop_id)
);
-- Display order of platforms along each line direction (most common full stop pattern).
CREATE TABLE IF NOT EXISTS normalized.ttc_line_stops (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    route_id text NOT NULL,
    direction_id smallint NOT NULL CHECK (direction_id IN (0, 1)),
    stop_order integer NOT NULL CHECK (stop_order > 0),
    stop_id text NOT NULL,
    towards text NOT NULL,
    PRIMARY KEY (feed_version, route_id, direction_id, stop_order),
    UNIQUE (feed_version, stop_id),
    FOREIGN KEY (feed_version, route_id) REFERENCES normalized.ttc_routes,
    FOREIGN KEY (feed_version, stop_id) REFERENCES normalized.ttc_stops
);
CREATE TABLE IF NOT EXISTS normalized.ttc_trips (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    trip_id text NOT NULL,
    route_id text NOT NULL,
    service_id text NOT NULL,
    direction_id smallint NOT NULL CHECK (direction_id IN (0, 1)),
    headsign text NOT NULL,
    PRIMARY KEY (feed_version, trip_id)
);
CREATE TABLE IF NOT EXISTS normalized.ttc_stop_times (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    trip_id text NOT NULL,
    stop_sequence integer NOT NULL,
    stop_id text NOT NULL,
    arrival_seconds integer,
    departure_seconds integer,
    PRIMARY KEY (feed_version, trip_id, stop_sequence)
);
CREATE INDEX IF NOT EXISTS ttc_stop_times_stop
  ON normalized.ttc_stop_times (feed_version, stop_id, arrival_seconds);
CREATE TABLE IF NOT EXISTS normalized.ttc_calendar (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    service_id text NOT NULL,
    monday boolean NOT NULL,
    tuesday boolean NOT NULL,
    wednesday boolean NOT NULL,
    thursday boolean NOT NULL,
    friday boolean NOT NULL,
    saturday boolean NOT NULL,
    sunday boolean NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    PRIMARY KEY (feed_version, service_id)
);
CREATE TABLE IF NOT EXISTS normalized.ttc_calendar_dates (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    service_id text NOT NULL,
    date date NOT NULL,
    exception_type smallint NOT NULL CHECK (exception_type IN (1, 2)),
    PRIMARY KEY (feed_version, service_id, date)
);

-- Realtime protobuf exactly as received. A repeated feed timestamp is the same snapshot,
-- so polling is idempotent. Short retention; normalized history is kept longer.
CREATE TABLE IF NOT EXISTS raw.ttc_realtime_snapshots (
    snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feed text NOT NULL CHECK (feed IN ('trips_subway', 'alerts_subway', 'alerts_accessibility')),
    feed_url text NOT NULL,
    feed_timestamp timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL,
    payload_sha256 text NOT NULL CHECK (length(payload_sha256) = 64),
    payload bytea NOT NULL,
    entity_count integer NOT NULL CHECK (entity_count >= 0),
    feed_version text REFERENCES normalized.ttc_gtfs_versions,
    normalized_at timestamptz,
    UNIQUE (feed, feed_timestamp)
);
CREATE INDEX IF NOT EXISTS ttc_snapshots_fetched ON raw.ttc_realtime_snapshots (fetched_at);

-- One row per train visit to a platform. trip_id is not a stable identity in TTC's feed
-- (the same train gets a new trip_id almost every poll), so visits are keyed by train
-- (vehicle label) and matched to a new prediction when within 10 minutes of it.
-- passed_at is the feed time of the first snapshot that still contains the train but no
-- longer lists this platform; predicted_arrival is then the best estimate of arrival.
CREATE TABLE IF NOT EXISTS normalized.ttc_train_stop_events (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    service_date date NOT NULL,
    train_id text NOT NULL,
    stop_id text NOT NULL,
    route_id text NOT NULL,
    direction_id smallint NOT NULL CHECK (direction_id IN (0, 1)),
    stop_order integer NOT NULL,
    predicted_arrival timestamptz NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL CHECK (last_seen_at >= first_seen_at),
    last_snapshot_id bigint NOT NULL,
    last_trip_id text NOT NULL,
    passed_at timestamptz CHECK (passed_at > last_seen_at)
);
CREATE INDEX IF NOT EXISTS ttc_events_train_stop
  ON normalized.ttc_train_stop_events (train_id, stop_id, predicted_arrival);
CREATE INDEX IF NOT EXISTS ttc_events_stop_time
  ON normalized.ttc_train_stop_events (stop_id, predicted_arrival);
CREATE INDEX IF NOT EXISTS ttc_events_line_time
  ON normalized.ttc_train_stop_events (route_id, direction_id, predicted_arrival);
CREATE INDEX IF NOT EXISTS ttc_events_last_seen
  ON normalized.ttc_train_stop_events (last_seen_at);

-- Predictions from the newest normalized trip snapshot only (replaced each poll).
CREATE TABLE IF NOT EXISTS normalized.ttc_current_predictions (
    train_id text NOT NULL,
    trip_id text NOT NULL,
    stop_id text NOT NULL,
    route_id text NOT NULL,
    direction_id smallint NOT NULL,
    direction_label text,
    stop_order integer NOT NULL,
    stop_sequence integer,
    predicted_arrival timestamptz NOT NULL,
    snapshot_id bigint NOT NULL,
    PRIMARY KEY (train_id, stop_id)
);

-- Alerts are versioned: any change in text, effect, periods or entities is a new version.
CREATE TABLE IF NOT EXISTS normalized.ttc_alerts (
    feed text NOT NULL,
    alert_id text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    last_snapshot_id bigint NOT NULL,
    current_version_hash text NOT NULL,
    PRIMARY KEY (feed, alert_id)
);
CREATE TABLE IF NOT EXISTS normalized.ttc_alert_versions (
    feed text NOT NULL,
    alert_id text NOT NULL,
    version_hash text NOT NULL CHECK (length(version_hash) = 64),
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    first_snapshot_id bigint NOT NULL,
    cause text NOT NULL,
    effect text NOT NULL,
    header_text text NOT NULL,
    description_text text,
    url text,
    active_periods jsonb NOT NULL,
    informed_entities jsonb NOT NULL,
    route_ids text[] NOT NULL,
    stop_ids text[] NOT NULL,
    effect_status text,
    text_status text,
    derived_status text NOT NULL,
    status_mismatch boolean NOT NULL,
    advance_notice boolean NOT NULL DEFAULT false,
    PRIMARY KEY (feed, alert_id, version_hash),
    FOREIGN KEY (feed, alert_id) REFERENCES normalized.ttc_alerts
);
-- Added after the first deployment; harmless when the column already exists.
ALTER TABLE normalized.ttc_alert_versions
  ADD COLUMN IF NOT EXISTS advance_notice boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS ttc_alert_versions_stops
  ON normalized.ttc_alert_versions USING gin (stop_ids);
CREATE INDEX IF NOT EXISTS ttc_alert_versions_routes
  ON normalized.ttc_alert_versions USING gin (route_ids);

-- Operations: one row per feed poll, the newest snapshot per feed, and record issues.
CREATE TABLE IF NOT EXISTS ops.ttc_poll_runs (
    poll_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    feed text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('stored', 'unchanged', 'failed')),
    snapshot_id bigint,
    feed_timestamp timestamptz,
    accepted integer,
    rejected integer,
    flagged integer,
    error text
);
CREATE INDEX IF NOT EXISTS ttc_poll_runs_feed_time ON ops.ttc_poll_runs (feed, started_at DESC);
CREATE TABLE IF NOT EXISTS ops.ttc_feed_state (
    feed text PRIMARY KEY,
    snapshot_id bigint NOT NULL,
    feed_timestamp timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS ops.ttc_record_issues (
    issue_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id bigint NOT NULL REFERENCES raw.ttc_realtime_snapshots ON DELETE CASCADE,
    feed text NOT NULL,
    issue text NOT NULL,
    action text NOT NULL CHECK (action IN ('rejected', 'flagged')),
    entity_id text,
    stop_id text,
    detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ttc_record_issues_snapshot ON ops.ttc_record_issues (snapshot_id);
CREATE TABLE IF NOT EXISTS ops.ttc_static_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    feed_version text,
    error text
);
