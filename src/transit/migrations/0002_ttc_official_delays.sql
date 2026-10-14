-- TTC's official subway delay log (Toronto Open Data, published monthly), used to check
-- this project's observed gaps against incidents TTC itself recorded.

-- Each distinct download, kept so normalization can be rerun after a matching fix.
CREATE TABLE raw.ttc_delay_files (
    file_sha256 text PRIMARY KEY CHECK (length(file_sha256) = 64),
    fetched_at timestamptz NOT NULL,
    delays_url text NOT NULL,
    codes_url text NOT NULL,
    delays_csv bytea NOT NULL,
    codes_csv bytea NOT NULL
);

-- The current file only: the log is republished in full, so a new file replaces the rows.
CREATE TABLE normalized.ttc_official_delays (
    source_id integer PRIMARY KEY,
    delay_at timestamptz NOT NULL,
    min_delay integer NOT NULL CHECK (min_delay >= 0),
    min_gap integer NOT NULL CHECK (min_gap >= 0),
    station_text text NOT NULL,
    station_key text,
    station_match text NOT NULL
        CHECK (station_match IN ('exact', 'alias', 'prefix', 'range', 'unmatched')),
    line_text text,
    route_id text,
    bound text CHECK (bound IN ('Northbound', 'Southbound', 'Eastbound', 'Westbound')),
    code text,
    vehicle text,
    file_sha256 text NOT NULL REFERENCES raw.ttc_delay_files
);
CREATE INDEX ttc_official_delays_time ON normalized.ttc_official_delays (delay_at);

CREATE TABLE normalized.ttc_delay_codes (
    code text PRIMARY KEY,
    description text NOT NULL,
    file_sha256 text NOT NULL REFERENCES raw.ttc_delay_files
);

CREATE TABLE ops.ttc_delay_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    file_sha256 text,
    rows integer,
    rows_with_gap integer,
    matched_with_gap integer,
    latest_delay_at timestamptz,
    error text
);
