-- City of Toronto food premises inspections (DineSafe; transit.food_inspections).

CREATE TABLE normalized.food_premises (
    premises_id text PRIMARY KEY,
    name text NOT NULL,
    address text NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    -- The last load that listed the premises; ones that close drop out of the city's file.
    last_seen_at timestamptz NOT NULL
);

-- One row per inspection. Status as published: Pass, Conditional Pass, Closed, or others
-- the city may add. Kept after an inspection leaves the city's two-to-three-year file.
CREATE TABLE normalized.food_inspections (
    premises_id text NOT NULL REFERENCES normalized.food_premises,
    inspected_on date NOT NULL,
    status text NOT NULL,
    PRIMARY KEY (premises_id, inspected_on)
);

-- Each infraction found, replaced whenever its inspection is in a load.
CREATE TABLE normalized.food_infractions (
    infraction_id text PRIMARY KEY,
    premises_id text NOT NULL,
    inspected_on date NOT NULL,
    infraction text NOT NULL,
    category text,
    severity text CHECK (severity IN ('minor', 'significant', 'crucial', 'not applicable')),
    -- Court outcome, if charged: for example Conviction - Fined, Pending, Charges Withdrawn.
    outcome text,
    outcome_on date,
    fine numeric(10, 2),
    FOREIGN KEY (premises_id, inspected_on) REFERENCES normalized.food_inspections
        ON DELETE CASCADE
);
CREATE INDEX ON normalized.food_infractions (premises_id, inspected_on);

-- The export is about 47 MB, so its body is not kept; its hash shows whether it changed.
CREATE TABLE ops.food_inspection_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    feed_sha256 text,
    premises integer,
    inspections integer,
    infractions integer,
    skipped integer,
    error text
);
