-- Air Quality Health Index for Toronto stations from ECCC (transit.air_quality).

CREATE TABLE normalized.aqhi_observations (
    location_id text NOT NULL,
    location_name text NOT NULL,
    observed_at timestamptz NOT NULL,
    aqhi numeric(5, 2) NOT NULL,
    PRIMARY KEY (location_id, observed_at)
);

-- Each station's newest forecast only, replaced on each load.
CREATE TABLE normalized.aqhi_forecasts (
    location_id text NOT NULL,
    published_at timestamptz NOT NULL,
    forecast_at timestamptz NOT NULL,
    aqhi numeric(5, 2) NOT NULL,
    PRIMARY KEY (location_id, forecast_at)
);

CREATE TABLE ops.aqhi_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    observations integer,
    forecasts integer,
    error text
);
