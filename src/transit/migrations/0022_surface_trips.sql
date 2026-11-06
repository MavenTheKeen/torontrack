-- Bus and streetcar trips one by one: every scheduled trip of a timetable version, and
-- every trip a vehicle reported running, so a scheduled trip no vehicle ran can be told
-- apart from a long gap. TTC's live trip IDs are those of its Complete GTFS.

CREATE TABLE normalized.ttc_surface_trips (
    feed_version text NOT NULL,
    trip_id text NOT NULL,
    pattern_id integer NOT NULL,
    service_id text NOT NULL,
    -- Seconds from the start of the service day, as in GTFS; they pass 86400 after
    -- midnight.
    start_seconds integer NOT NULL,
    end_seconds integer NOT NULL,
    PRIMARY KEY (feed_version, trip_id),
    FOREIGN KEY (feed_version, pattern_id) REFERENCES normalized.ttc_surface_patterns
        ON DELETE CASCADE
);

-- One row per trip and service day (04:00 to 04:00 Toronto time) with a vehicle on it,
-- updated from every vehicle snapshot. Kept TTC_EVENT_RETENTION_DAYS, like passages.
CREATE TABLE normalized.ttc_surface_trips_seen (
    service_date date NOT NULL,
    trip_id text NOT NULL,
    route_id text NOT NULL,
    -- The vehicle last reported on the trip.
    vehicle_id text NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    snapshots integer NOT NULL,
    PRIMARY KEY (service_date, trip_id)
);
