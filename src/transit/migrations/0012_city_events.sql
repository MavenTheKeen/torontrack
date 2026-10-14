-- City of Toronto festivals and events calendar (transit.events).

CREATE TABLE normalized.city_events (
    event_id text PRIMARY KEY,
    name text NOT NULL,
    categories text[] NOT NULL DEFAULT '{}',
    featured boolean NOT NULL,
    free boolean,
    website text,
    venue text,
    address text,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    source_modified_at timestamptz,
    last_seen_at timestamptz NOT NULL
);

-- Each date an event takes place. Future dates are replaced on every load (so a cancelled
-- date disappears); past dates are kept as history.
CREATE TABLE normalized.city_event_occurrences (
    event_id text NOT NULL REFERENCES normalized.city_events ON DELETE CASCADE,
    starts_at timestamptz NOT NULL,
    PRIMARY KEY (event_id, starts_at)
);
CREATE INDEX ON normalized.city_event_occurrences (starts_at);

-- The feed is 225 MB, so its body is not kept; its hash shows whether it changed.
CREATE TABLE ops.city_event_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    feed_sha256 text,
    events integer,
    occurrences integer,
    skipped jsonb,
    error text
);
