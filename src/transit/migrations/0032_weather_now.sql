-- The weather map's current weather (transit.weather_now): ECCC's city page summary and
-- radar images of the Toronto area, kept for two hours.

CREATE TABLE normalized.weather_citypage (
    city text PRIMARY KEY,
    fetched_at timestamptz NOT NULL,
    summary jsonb NOT NULL
);

CREATE TABLE normalized.radar_frames (
    kind text NOT NULL CHECK (kind IN ('rain', 'snow')),
    frame_at timestamptz NOT NULL,
    png bytea NOT NULL,
    PRIMARY KEY (kind, frame_at)
);

CREATE TABLE ops.weather_now_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    frames integer NOT NULL,
    error text
);
