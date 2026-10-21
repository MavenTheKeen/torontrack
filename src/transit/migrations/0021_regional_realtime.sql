-- Live regional trains from Metrolinx's GTFS-Realtime API (transit/regional_live.py):
-- the latest position and delay of each running train, its predicted times at the
-- stations ahead, and current service alerts. Only the latest poll is kept, so the tables
-- skip the write-ahead log like the other current-state tables (0019).

CREATE UNLOGGED TABLE normalized.regional_live_trains (
    trip_id text PRIMARY KEY,
    line_id text NOT NULL,
    vehicle_id text,
    lat double precision,
    lon double precision,
    reported_at timestamptz,
    -- Seconds behind the timetable at the next station, from the trip update, and when
    -- that update was published.
    delay_seconds integer,
    delay_at timestamptz,
    cancelled boolean NOT NULL DEFAULT false,
    -- Last poll that listed the train in either feed; finished trains are then removed.
    updated_at timestamptz NOT NULL
);

CREATE UNLOGGED TABLE normalized.regional_live_stops (
    trip_id text NOT NULL,
    station_id text NOT NULL,
    predicted_departure timestamptz,
    skipped boolean NOT NULL DEFAULT false,
    PRIMARY KEY (trip_id, station_id)
);

CREATE UNLOGGED TABLE normalized.regional_alerts (
    alert_id text PRIMARY KEY,
    effect text NOT NULL,
    header text NOT NULL,
    description text,
    url text,
    line_ids text[] NOT NULL DEFAULT '{}',
    station_ids text[] NOT NULL DEFAULT '{}',
    active_from timestamptz,
    active_until timestamptz
);

CREATE TABLE ops.regional_polls (
    feed text PRIMARY KEY CHECK (feed IN ('vehicles', 'trips', 'alerts')),
    polled_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('ok', 'failed')),
    -- The last successful poll, kept through failures for the health check.
    succeeded_at timestamptz,
    rows integer,
    feed_timestamp timestamptz,
    error text
);
