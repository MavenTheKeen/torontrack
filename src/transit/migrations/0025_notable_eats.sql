-- Notable places to eat from Wikidata (CC0; transit.food_notable), replaced on each load.

CREATE TABLE normalized.notable_eats (
    wikidata_id text PRIMARY KEY,
    name text NOT NULL,
    lat double precision NOT NULL,
    lon double precision NOT NULL,
    opened integer,
    wikipedia text,
    -- Michelin Guide awards recorded in Wikidata, separated by "; ".
    awards text,
    loaded_at timestamptz NOT NULL
);

CREATE TABLE ops.notable_eat_refreshes (
    refresh_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('loaded', 'failed')),
    places integer,
    error text
);
