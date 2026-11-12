-- CaféTO patio locations (transit.patios), replaced on each load.

CREATE TABLE normalized.cafeto_patios (
    licence_no text,
    operator text NOT NULL,
    address text,
    -- Curb Lane, Sidewalk, Parklet, Private Property Patio and so on, as published.
    kind text,
    lon double precision NOT NULL,
    lat double precision NOT NULL
);

CREATE TABLE ops.cafeto_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    patios integer,
    error text
);
