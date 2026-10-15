-- Concerts, games and shows from Ticketmaster's Discovery API (transit.ticketmaster).
-- Ticketmaster's terms allow caching event content only for reasonable periods to provide
-- the service, so only upcoming events are kept: each load replaces them, and past events
-- are deleted after a day.

CREATE TABLE normalized.ticketed_events (
    event_id text PRIMARY KEY,
    name text NOT NULL,
    segment text,
    genre text,
    starts_at timestamptz,
    local_date date NOT NULL,
    time_tbd boolean NOT NULL,
    status text NOT NULL,
    venue_id text,
    venue text,
    address text,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    url text,
    loaded_at timestamptz NOT NULL
);
CREATE INDEX ON normalized.ticketed_events (local_date);

CREATE TABLE ops.ticketed_event_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'skipped', 'failed')),
    requests integer,
    events integer,
    skipped jsonb,
    error text
);
