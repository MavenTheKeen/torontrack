-- City of Toronto business licences of places that serve food (transit.food_licences).
-- No holder names or phone numbers are kept.

CREATE TABLE normalized.food_licences (
    licence_no text PRIMARY KEY,
    category text NOT NULL,
    operating_name text,
    -- Street address without unit, postal code or punctuation: 2300 YONGE ST.
    address_key text NOT NULL,
    issued_on date NOT NULL,
    cancelled_on date,
    last_seen_at timestamptz NOT NULL
);
CREATE INDEX ON normalized.food_licences (address_key);

CREATE TABLE ops.food_licence_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    feed_sha256 text,
    licences integer,
    skipped integer,
    error text
);
