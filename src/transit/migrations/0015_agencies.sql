-- Neighbouring bus agencies (transit.agencies): their stop and route network from static
-- GTFS, one current version per agency, and their buses' latest live positions.

CREATE TABLE normalized.agency_versions (
    agency text NOT NULL,
    feed_version text NOT NULL,
    feed_sha256 text NOT NULL,
    source_url text NOT NULL,
    loaded_at timestamptz NOT NULL,
    PRIMARY KEY (agency, feed_version)
);

CREATE TABLE normalized.agency_routes (
    agency text NOT NULL,
    feed_version text NOT NULL,
    route_id text NOT NULL,
    short_name text NOT NULL,
    long_name text NOT NULL,
    PRIMARY KEY (agency, feed_version, route_id),
    FOREIGN KEY (agency, feed_version) REFERENCES normalized.agency_versions ON DELETE CASCADE
);

CREATE TABLE normalized.agency_stops (
    agency text NOT NULL,
    feed_version text NOT NULL,
    stop_id text NOT NULL,
    stop_code text,
    name text NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    PRIMARY KEY (agency, feed_version, stop_id),
    FOREIGN KEY (agency, feed_version) REFERENCES normalized.agency_versions ON DELETE CASCADE
);

CREATE TABLE normalized.agency_patterns (
    agency text NOT NULL,
    feed_version text NOT NULL,
    pattern_id integer NOT NULL,
    route_id text NOT NULL,
    direction_id smallint NOT NULL,
    towards text NOT NULL,
    trips integer NOT NULL,
    PRIMARY KEY (agency, feed_version, pattern_id),
    FOREIGN KEY (agency, feed_version) REFERENCES normalized.agency_versions ON DELETE CASCADE
);

-- Stops in order along each pattern, with the average scheduled seconds from its start.
CREATE TABLE normalized.agency_pattern_stops (
    agency text NOT NULL,
    feed_version text NOT NULL,
    pattern_id integer NOT NULL,
    stop_order integer NOT NULL,
    stop_id text NOT NULL,
    offset_seconds integer NOT NULL,
    PRIMARY KEY (agency, feed_version, pattern_id, stop_order),
    FOREIGN KEY (agency, feed_version) REFERENCES normalized.agency_versions ON DELETE CASCADE
);

-- Each bus's latest report only; reports over an hour old are deleted on each poll.
CREATE TABLE normalized.agency_vehicles (
    agency text NOT NULL,
    vehicle_id text NOT NULL,
    route_id text,
    trip_id text,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    bearing real,
    speed_mps real,
    occupancy text,
    reported_at timestamptz NOT NULL,
    PRIMARY KEY (agency, vehicle_id)
);

CREATE TABLE ops.agency_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agency text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    feed_version text,
    routes integer,
    stops integer,
    patterns integer,
    error text
);

-- The latest live poll of each agency, for health checks.
CREATE TABLE ops.agency_polls (
    agency text PRIMARY KEY,
    polled_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('ok', 'failed')),
    vehicles integer,
    feed_timestamp timestamptz,
    error text
);
