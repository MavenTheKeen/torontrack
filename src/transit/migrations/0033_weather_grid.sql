-- The weather map's hourly wind and precipitation forecast for the Toronto area
-- (transit.weather_now): ECCC's High Resolution Deterministic Prediction System, one row
-- per model run kept, replaced when a newer run is read.

CREATE TABLE normalized.weather_grid (
    area text PRIMARY KEY,
    run_at timestamptz NOT NULL,
    fetched_at timestamptz NOT NULL,
    grid jsonb NOT NULL
);
