-- City wards and daily counts of 311 service requests (transit.neighbourhood). Requests are
-- kept only as counts per day, ward and type: no row identifies a caller or an address.

CREATE TABLE normalized.city_wards (
    ward smallint PRIMARY KEY,
    name text NOT NULL,
    min_lon double precision NOT NULL,
    min_lat double precision NOT NULL,
    max_lon double precision NOT NULL,
    max_lat double precision NOT NULL,
    rings jsonb NOT NULL
);

CREATE TABLE normalized.service_requests_daily (
    day date NOT NULL,
    -- 0 when the request has no ward.
    ward smallint NOT NULL,
    request_type text NOT NULL,
    division text NOT NULL,
    requests integer NOT NULL,
    PRIMARY KEY (day, ward, request_type, division)
);
CREATE INDEX ON normalized.service_requests_daily (ward, day);

CREATE TABLE ops.service_request_files (
    year integer PRIMARY KEY,
    sha256 text NOT NULL,
    loaded_at timestamptz NOT NULL,
    requests integer NOT NULL,
    skipped integer NOT NULL
);

CREATE TABLE ops.service_request_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'unchanged', 'failed')),
    years jsonb NOT NULL,
    error text
);
