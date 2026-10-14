-- Buses and streetcars (transit.ttc.surface): their routes, stops and stop patterns from
-- static GTFS, and the latest position of every vehicle from TTC's vehicle feed.

ALTER TABLE raw.ttc_realtime_snapshots DROP CONSTRAINT ttc_realtime_snapshots_feed_check;
ALTER TABLE raw.ttc_realtime_snapshots ADD CONSTRAINT ttc_realtime_snapshots_feed_check
    CHECK (feed IN ('trips_subway', 'alerts_subway', 'alerts_accessibility', 'vehicles'));

-- GTFS route_type 3 is bus; 0 is streetcar and light rail.
CREATE TABLE normalized.ttc_surface_routes (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    route_id text NOT NULL,
    short_name text NOT NULL,
    long_name text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('bus', 'streetcar')),
    PRIMARY KEY (feed_version, route_id)
);

CREATE TABLE normalized.ttc_surface_stops (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    stop_id text NOT NULL,
    stop_code text,
    name text NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    PRIMARY KEY (feed_version, stop_id)
);

-- A pattern is one exact sequence of stops on a route and direction. offset_seconds is the
-- mean scheduled time from the pattern's first stop, across every trip using it.
CREATE TABLE normalized.ttc_surface_patterns (
    feed_version text NOT NULL REFERENCES normalized.ttc_gtfs_versions ON DELETE CASCADE,
    pattern_id integer NOT NULL,
    route_id text NOT NULL,
    direction_id smallint NOT NULL CHECK (direction_id IN (0, 1)),
    towards text NOT NULL,
    trips integer NOT NULL CHECK (trips > 0),
    PRIMARY KEY (feed_version, pattern_id)
);

CREATE TABLE normalized.ttc_surface_pattern_stops (
    feed_version text NOT NULL,
    pattern_id integer NOT NULL,
    stop_order smallint NOT NULL,
    stop_id text NOT NULL,
    offset_seconds integer NOT NULL CHECK (offset_seconds >= 0),
    PRIMARY KEY (feed_version, pattern_id, stop_order),
    FOREIGN KEY (feed_version, pattern_id)
        REFERENCES normalized.ttc_surface_patterns ON DELETE CASCADE
);
CREATE INDEX ttc_surface_pattern_stops_stop
    ON normalized.ttc_surface_pattern_stops (feed_version, stop_id);

-- Latest position of each vehicle in revenue service, replaced by every new snapshot.
-- Neither the realtime trip_id nor stop_id matches static GTFS IDs (both are kept as
-- reported), so the site places a vehicle on its route by position instead.
-- History is not kept: at ~1,600 vehicles every 30 seconds it would add ~400 MB a day.
CREATE TABLE normalized.ttc_vehicles (
    vehicle_id text PRIMARY KEY,
    route_id text NOT NULL,
    trip_id text,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    bearing smallint,
    speed_mps real,
    stop_id text,
    status text,
    occupancy text,
    reported_at timestamptz NOT NULL,
    snapshot_id bigint NOT NULL
);
