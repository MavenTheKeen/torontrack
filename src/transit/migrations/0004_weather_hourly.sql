-- Hourly weather observations from Environment and Climate Change Canada (transit.weather),
-- joined to subway and Bike Share data by hour.

-- Each response, kept so normalization can be rerun.
CREATE TABLE raw.weather_responses (
    response_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fetched_at timestamptz NOT NULL,
    url text NOT NULL,
    payload jsonb NOT NULL
);

-- One row per station and hour. ECCC can revise an hour after quality control, so later
-- fetches overwrite it; response_id points at the response that supplied the values.
CREATE TABLE normalized.weather_hourly (
    climate_id text NOT NULL,
    observed_at timestamptz NOT NULL,
    station_name text NOT NULL,
    temp_c numeric(4, 1),
    dew_point_c numeric(4, 1),
    relative_humidity smallint,
    precip_mm numeric(5, 1),
    station_pressure_kpa numeric(5, 2),
    wind_speed_kmh smallint,
    wind_direction_10deg smallint,
    visibility_km numeric(5, 1),
    humidex smallint,
    windchill smallint,
    weather text,
    -- ECCC data-quality flags for any value (e.g. estimated, partial, missing).
    flags jsonb NOT NULL DEFAULT '{}',
    response_id bigint NOT NULL REFERENCES raw.weather_responses,
    PRIMARY KEY (climate_id, observed_at)
);

CREATE TABLE ops.weather_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    hours integer,
    latest_observation timestamptz,
    error text
);
