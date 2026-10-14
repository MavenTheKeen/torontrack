-- Bike Share Toronto ridership: every trip, from the city's yearly open-data files
-- (transit.bikeshare.trips). Trips are not stored one by one (about 7 million a year);
-- each year is loaded into the summaries below, replacing that year's rows.

-- Each yearly file loaded, with what validation found in it.
CREATE TABLE normalized.bikeshare_trip_files (
    year smallint PRIMARY KEY,
    resource_name text NOT NULL,
    url text NOT NULL,
    source_modified text,
    file_sha256 text NOT NULL,
    loaded_at timestamptz NOT NULL,
    trips integer NOT NULL,
    first_trip date,
    last_trip date,
    -- Rows dropped: no start dock or time, unreadable duration, or under 60 seconds.
    skipped jsonb NOT NULL,
    -- Rows whose end dock name repeats the start dock's although the dock IDs differ.
    end_name_copied integer NOT NULL
);

-- Departures and arrivals per dock and calendar day (Toronto time, as in the file).
CREATE TABLE normalized.bikeshare_trip_station_days (
    trip_date date NOT NULL,
    station_id text NOT NULL,
    departures integer NOT NULL,
    arrivals integer NOT NULL,
    member_departures integer NOT NULL,
    ebike_departures integer NOT NULL,
    round_trips integer NOT NULL,
    PRIMARY KEY (trip_date, station_id)
);

-- Departures and arrivals per dock, month, weekday or weekend, and hour of day.
CREATE TABLE normalized.bikeshare_trip_station_hours (
    month date NOT NULL,
    station_id text NOT NULL,
    day_type text NOT NULL CHECK (day_type IN ('weekday', 'weekend')),
    hour smallint NOT NULL,
    departures integer NOT NULL,
    arrivals integer NOT NULL,
    PRIMARY KEY (month, station_id, day_type, hour)
);

-- Trips per pair of docks and year, with total duration of trips between 1 minute and
-- 3 hours for a typical duration.
CREATE TABLE normalized.bikeshare_trip_pairs (
    year smallint NOT NULL,
    start_station_id text NOT NULL,
    end_station_id text NOT NULL,
    trips integer NOT NULL,
    member_trips integer NOT NULL,
    ebike_trips integer NOT NULL,
    timed_trips integer NOT NULL,
    timed_seconds bigint NOT NULL,
    PRIMARY KEY (year, start_station_id, end_station_id)
);
CREATE INDEX ON normalized.bikeshare_trip_pairs (start_station_id, year);
CREATE INDEX ON normalized.bikeshare_trip_pairs (end_station_id, year);

-- The newest name each dock was recorded under as a trip's start.
CREATE TABLE normalized.bikeshare_trip_stations (
    station_id text PRIMARY KEY,
    name text NOT NULL,
    last_trip date NOT NULL
);

CREATE TABLE ops.bikeshare_trip_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    years_loaded smallint[],
    error text
);
