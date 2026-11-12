-- Toronto beach water quality: every E. coli sample since 2007 (transit.beaches),
-- replaced on each load.

CREATE TABLE normalized.beach_samples (
    beach_id integer NOT NULL,
    beach_name text NOT NULL,
    site text NOT NULL,
    day date NOT NULL,
    -- E. coli per 100 mL.
    ecoli integer NOT NULL,
    lon double precision NOT NULL,
    lat double precision NOT NULL
);
CREATE INDEX ON normalized.beach_samples (beach_id, day);

CREATE TABLE ops.beach_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    samples integer,
    error text
);
