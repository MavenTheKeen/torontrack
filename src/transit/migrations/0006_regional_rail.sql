-- Regional rail schedule from Metrolinx static GTFS (transit.regional). Rows are keyed
-- by feed_version (the zip's SHA-256), like the TTC schedule. Trains only: buses and the
-- airport rail link are left out.

CREATE TABLE normalized.regional_versions (
    feed_version text PRIMARY KEY,
    published_version text,
    service_start date,
    service_end date,
    source_url text NOT NULL,
    loaded_at timestamptz NOT NULL
);

CREATE TABLE normalized.regional_lines (
    feed_version text NOT NULL REFERENCES normalized.regional_versions ON DELETE CASCADE,
    line_id text NOT NULL,
    code text NOT NULL,
    name text NOT NULL,
    colour text,
    text_colour text,
    PRIMARY KEY (feed_version, line_id)
);

CREATE TABLE normalized.regional_stations (
    feed_version text NOT NULL REFERENCES normalized.regional_versions ON DELETE CASCADE,
    station_id text NOT NULL,
    name text NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    PRIMARY KEY (feed_version, station_id)
);

-- Track geometry as [[lon, lat], ...]; trips without a shape are drawn station to station.
CREATE TABLE normalized.regional_shapes (
    feed_version text NOT NULL REFERENCES normalized.regional_versions ON DELETE CASCADE,
    shape_id text NOT NULL,
    path jsonb NOT NULL,
    PRIMARY KEY (feed_version, shape_id)
);

CREATE TABLE normalized.regional_service_dates (
    feed_version text NOT NULL REFERENCES normalized.regional_versions ON DELETE CASCADE,
    service_id text NOT NULL,
    service_date date NOT NULL,
    PRIMARY KEY (feed_version, service_date, service_id)
);

CREATE TABLE normalized.regional_trips (
    feed_version text NOT NULL REFERENCES normalized.regional_versions ON DELETE CASCADE,
    trip_id text NOT NULL,
    line_id text NOT NULL,
    service_id text NOT NULL,
    direction_id smallint,
    destination text NOT NULL,
    shape_id text,
    PRIMARY KEY (feed_version, trip_id),
    FOREIGN KEY (feed_version, line_id) REFERENCES normalized.regional_lines
);
CREATE INDEX ON normalized.regional_trips (feed_version, service_id);

-- Seconds after the service day's local noon minus 12 hours (GTFS time; can pass 24:00).
CREATE TABLE normalized.regional_stop_times (
    feed_version text NOT NULL,
    trip_id text NOT NULL,
    stop_sequence integer NOT NULL,
    station_id text NOT NULL,
    arrival_s integer NOT NULL,
    departure_s integer NOT NULL,
    PRIMARY KEY (feed_version, trip_id, stop_sequence),
    FOREIGN KEY (feed_version, trip_id) REFERENCES normalized.regional_trips ON DELETE CASCADE,
    FOREIGN KEY (feed_version, station_id) REFERENCES normalized.regional_stations
);

CREATE TABLE ops.regional_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    feed_version text,
    error text
);
