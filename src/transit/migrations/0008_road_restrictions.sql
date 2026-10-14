-- City of Toronto road restrictions: construction zones and road closures (transit.roads).

-- Each distinct response body, kept so normalization can be rerun. Unchanged responses
-- are not stored again.
CREATE TABLE raw.road_restriction_responses (
    response_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    fetched_at timestamptz NOT NULL,
    sha256 text NOT NULL UNIQUE,
    body text NOT NULL
);

-- One row per restriction ID, updated from each changed response. removed_at is set when
-- the restriction leaves the feed, and cleared if it comes back.
CREATE TABLE normalized.road_restrictions (
    restriction_id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('construction', 'closure')),
    road text,
    name text NOT NULL,
    district text,
    road_class text,
    description text,
    directions text,
    work_period text,
    max_impact text,
    current_impact text,
    -- Weekly hours as published, e.g. {"monday": "07:00-18:00", "everyday": null}.
    schedule jsonb NOT NULL DEFAULT '{}',
    starts_at timestamptz NOT NULL,
    ends_at timestamptz NOT NULL,
    -- [[lon, lat], ...]
    path jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    removed_at timestamptz,
    source_updated_at timestamptz,
    response_id bigint NOT NULL
);
CREATE INDEX ON normalized.road_restrictions (ends_at) WHERE removed_at IS NULL;

CREATE TABLE ops.road_restriction_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'dropout', 'failed')),
    restrictions integer,
    skipped integer,
    error text
);
