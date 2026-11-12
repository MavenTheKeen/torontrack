-- City of Toronto recreation facilities and drop-in skating and swimming sessions
-- (transit.recreation), replaced on each load.

CREATE TABLE normalized.rec_facilities (
    location_id integer PRIMARY KEY,
    name text NOT NULL,
    facility_type text,
    address text,
    url text,
    lon double precision NOT NULL,
    lat double precision NOT NULL
);

CREATE TABLE normalized.rec_sessions (
    location_id integer NOT NULL,
    kind text NOT NULL CHECK (kind IN ('skate', 'swim')),
    title text NOT NULL,
    day date NOT NULL,
    starts time NOT NULL,
    ends time NOT NULL,
    age_min smallint,
    age_max smallint
);
CREATE INDEX ON normalized.rec_sessions (day);

CREATE TABLE ops.rec_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    facilities integer,
    sessions integer,
    error text
);
