-- City of Toronto curbside collection areas and pickup schedule (transit.waste), replaced on
-- each load.

CREATE TABLE normalized.waste_areas (
    area_id text PRIMARY KEY,
    -- As spelled in the schedule (Tuesday1), or No Collection.
    calendar text NOT NULL,
    district text,
    min_lon double precision NOT NULL,
    min_lat double precision NOT NULL,
    max_lon double precision NOT NULL,
    max_lat double precision NOT NULL,
    -- Polygon rings as [[lon, lat], ...]; a point is inside by the even-odd rule.
    rings jsonb NOT NULL
);

CREATE TABLE normalized.waste_collections (
    calendar text NOT NULL,
    collected_on date NOT NULL,
    items text[] NOT NULL,
    PRIMARY KEY (calendar, collected_on)
);

CREATE TABLE ops.waste_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    areas integer,
    collections integer,
    error text
);
