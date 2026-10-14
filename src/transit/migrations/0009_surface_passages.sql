-- Bus and streetcar stop passages, recorded from live positions (transit.ttc.passages),
-- and scheduled trip starts to compare their headways with.

-- Scheduled trips per stop pattern, service ID and hour of the trip's first departure
-- (GTFS hours, which pass 24 after midnight).
CREATE TABLE normalized.ttc_surface_pattern_starts (
    feed_version text NOT NULL,
    pattern_id integer NOT NULL,
    service_id text NOT NULL,
    start_hour smallint NOT NULL,
    trips integer NOT NULL,
    PRIMARY KEY (feed_version, pattern_id, service_id, start_hour),
    FOREIGN KEY (feed_version, pattern_id) REFERENCES normalized.ttc_surface_patterns
        ON DELETE CASCADE
);

-- One row each time a matched vehicle passes a measurement stop (about one per 10
-- scheduled minutes of its pattern). The time is interpolated between the two position
-- reports either side of the stop. Kept TTC_EVENT_RETENTION_DAYS, like train visits.
CREATE TABLE normalized.ttc_surface_passages (
    passed_at timestamptz NOT NULL,
    route_id text NOT NULL,
    direction_id smallint NOT NULL,
    stop_id text NOT NULL,
    vehicle_id text NOT NULL,
    pattern_id integer NOT NULL,
    -- Seconds between the two position reports the time was interpolated from.
    report_gap_seconds smallint NOT NULL
);
CREATE INDEX ttc_surface_passages_time ON normalized.ttc_surface_passages (passed_at);

-- Where each vehicle was last placed on its pattern, to detect the next stop crossings.
CREATE TABLE ops.ttc_vehicle_progress (
    vehicle_id text PRIMARY KEY,
    feed_version text NOT NULL,
    pattern_id integer NOT NULL,
    position double precision NOT NULL,
    reported_at timestamptz NOT NULL
);
