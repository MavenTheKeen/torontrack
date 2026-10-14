-- Store Bike Share data without repeating what does not change. Lossless:
--   * raw: each payload's "data" member is stored once per distinct content in
--     raw.payload_bodies; raw.feed_payloads keeps one row per fetch with the envelope.
--     Station information is identical from run to run except its timestamp.
--   * station metadata: one row per distinct (name, location, capacity) version instead
--     of one per collection and station (97% of those rows were repeats).
--   * observations: per-collection values (collection ID, fetch and publication times,
--     raw lineage) move to normalized.collections under a small integer key.
-- normalized.station_observations rebuilds the previous one-row-per-metadata-station
-- shape for readers. Every step is checked before the old tables are dropped.

-- Raw payloads ---------------------------------------------------------------------

CREATE TABLE raw.payload_bodies (
    body_hash text PRIMARY KEY CHECK (length(body_hash) = 64),
    body jsonb NOT NULL
);

-- jsonb's text form is canonical, so equal content always hashes equally.
CREATE FUNCTION raw.body_hash(body jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    RETURN encode(sha256(convert_to(body::text, 'UTF8')), 'hex');

ALTER TABLE raw.feed_payloads
    ADD COLUMN envelope jsonb,
    ADD COLUMN body_hash text REFERENCES raw.payload_bodies;

INSERT INTO raw.payload_bodies (body_hash, body)
SELECT DISTINCT ON (raw.body_hash(payload -> 'data')) raw.body_hash(payload -> 'data'),
       payload -> 'data'
FROM raw.feed_payloads
WHERE jsonb_typeof(payload) = 'object' AND payload ? 'data';

UPDATE raw.feed_payloads SET
    envelope = CASE WHEN jsonb_typeof(payload) = 'object' AND payload ? 'data'
                    THEN payload - 'data' ELSE payload END,
    body_hash = CASE WHEN jsonb_typeof(payload) = 'object' AND payload ? 'data'
                     THEN raw.body_hash(payload -> 'data') END;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM raw.feed_payloads f
        LEFT JOIN raw.payload_bodies b USING (body_hash)
        WHERE (CASE WHEN f.body_hash IS NULL THEN f.envelope
                    ELSE f.envelope || jsonb_build_object('data', b.body) END)
              IS DISTINCT FROM f.payload
    ) THEN
        RAISE EXCEPTION 'raw payload reconstruction differs from the stored payload';
    END IF;
END $$;

ALTER TABLE raw.feed_payloads DROP COLUMN payload;
ALTER TABLE raw.feed_payloads ALTER COLUMN envelope SET NOT NULL;

CREATE VIEW raw.feed_payloads_full AS
SELECT f.*,
       CASE WHEN f.body_hash IS NULL THEN f.envelope
            ELSE f.envelope || jsonb_build_object('data', b.body) END AS payload
FROM raw.feed_payloads f
LEFT JOIN raw.payload_bodies b USING (body_hash);
COMMENT ON VIEW raw.feed_payloads_full IS
    'Each fetched payload exactly as received: envelope plus its deduplicated data body.';

-- Normalized -----------------------------------------------------------------------

CREATE TABLE normalized.collections (
    collection_key integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    collection_id text NOT NULL UNIQUE REFERENCES ops.ingestion_runs,
    collected_at timestamptz NOT NULL,
    status_fetched_at timestamptz NOT NULL,
    status_published_at timestamptz NOT NULL,
    information_raw_id bigint NOT NULL REFERENCES raw.feed_payloads,
    status_raw_id bigint NOT NULL REFERENCES raw.feed_payloads
);

CREATE TABLE normalized.station_versions (
    station_version_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lat double precision NOT NULL CHECK (lat BETWEEN -90 AND 90),
    lon double precision NOT NULL CHECK (lon BETWEEN -180 AND 180),
    capacity integer CHECK (capacity >= 0),
    station_id text NOT NULL,
    name text NOT NULL,
    UNIQUE NULLS NOT DISTINCT (station_id, name, lat, lon, capacity),
    UNIQUE (station_version_id, station_id)
);

-- Fixed-width columns first keeps rows free of alignment padding.
CREATE TABLE normalized.observations_compact (
    collection_key integer NOT NULL REFERENCES normalized.collections ON DELETE CASCADE,
    station_version_id integer NOT NULL,
    station_reported_at timestamptz NOT NULL,
    num_bikes_available integer NOT NULL CHECK (num_bikes_available >= 0),
    num_docks_available integer NOT NULL CHECK (num_docks_available >= 0),
    num_bikes_disabled integer CHECK (num_bikes_disabled >= 0),
    num_docks_disabled integer CHECK (num_docks_disabled >= 0),
    is_installed boolean NOT NULL,
    is_renting boolean NOT NULL,
    is_returning boolean NOT NULL,
    station_id text NOT NULL,
    PRIMARY KEY (collection_key, station_id),
    FOREIGN KEY (station_version_id, station_id)
        REFERENCES normalized.station_versions (station_version_id, station_id)
);

-- Stations listed in a collection's metadata without a status report. Kept so coverage
-- (metadata stations vs observed stations) stays exact.
CREATE TABLE normalized.unobserved_stations (
    collection_key integer NOT NULL REFERENCES normalized.collections ON DELETE CASCADE,
    station_version_id integer NOT NULL,
    station_id text NOT NULL,
    PRIMARY KEY (collection_key, station_id),
    FOREIGN KEY (station_version_id, station_id)
        REFERENCES normalized.station_versions (station_version_id, station_id)
);

-- Per-collection values were written identically on every row of a collection.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM normalized.observations GROUP BY collection_id
        HAVING count(DISTINCT (collected_at, status_fetched_at, source_published_at,
                               status_raw_id)) > 1
    ) OR EXISTS (
        SELECT 1 FROM normalized.station_snapshots GROUP BY collection_id
        HAVING count(DISTINCT information_raw_id) > 1
    ) THEN
        RAISE EXCEPTION 'per-collection values differ within a collection';
    END IF;
END $$;

INSERT INTO normalized.collections
    (collection_id, collected_at, status_fetched_at, status_published_at,
     information_raw_id, status_raw_id)
SELECT s.collection_id,
       coalesce(o.collected_at, r.collected_at),
       coalesce(o.status_fetched_at, p.fetched_at),
       coalesce(o.source_published_at, p.source_published_at),
       s.information_raw_id,
       coalesce(o.status_raw_id, p.raw_id)
FROM (
    SELECT collection_id, min(information_raw_id) AS information_raw_id
    FROM normalized.station_snapshots GROUP BY collection_id
) s
JOIN ops.ingestion_runs r USING (collection_id)
LEFT JOIN LATERAL (
    SELECT collected_at, status_fetched_at, source_published_at, status_raw_id
    FROM normalized.observations WHERE collection_id = s.collection_id LIMIT 1
) o ON true
LEFT JOIN raw.feed_payloads p
    ON p.collection_id = s.collection_id AND p.feed_name = 'station_status'
ORDER BY coalesce(o.collected_at, r.collected_at), s.collection_id;

INSERT INTO normalized.station_versions (station_id, name, lat, lon, capacity)
SELECT station_id, name, lat, lon, capacity
FROM normalized.station_snapshots s
JOIN normalized.collections c USING (collection_id)
GROUP BY station_id, name, lat, lon, capacity
ORDER BY min(c.collection_key), station_id;

INSERT INTO normalized.observations_compact
    (collection_key, station_version_id, station_reported_at, num_bikes_available,
     num_docks_available, num_bikes_disabled, num_docks_disabled, is_installed,
     is_renting, is_returning, station_id)
SELECT c.collection_key, v.station_version_id, o.station_reported_at,
       o.num_bikes_available, o.num_docks_available, o.num_bikes_disabled,
       o.num_docks_disabled, o.is_installed, o.is_renting, o.is_returning, o.station_id
FROM normalized.observations o
JOIN normalized.station_snapshots s USING (collection_id, station_id)
JOIN normalized.collections c USING (collection_id)
JOIN normalized.station_versions v
    ON v.station_id = s.station_id AND v.name = s.name AND v.lat = s.lat
   AND v.lon = s.lon AND v.capacity IS NOT DISTINCT FROM s.capacity;

INSERT INTO normalized.unobserved_stations (collection_key, station_version_id, station_id)
SELECT c.collection_key, v.station_version_id, s.station_id
FROM normalized.station_snapshots s
JOIN normalized.collections c USING (collection_id)
JOIN normalized.station_versions v
    ON v.station_id = s.station_id AND v.name = s.name AND v.lat = s.lat
   AND v.lon = s.lon AND v.capacity IS NOT DISTINCT FROM s.capacity
WHERE NOT EXISTS (
    SELECT 1 FROM normalized.observations o
    WHERE o.collection_id = s.collection_id AND o.station_id = s.station_id
);

DO $$
BEGIN
    IF (SELECT count(*) FROM normalized.observations_compact)
           <> (SELECT count(*) FROM normalized.observations)
       OR (SELECT count(*) FROM normalized.observations_compact)
          + (SELECT count(*) FROM normalized.unobserved_stations)
           <> (SELECT count(*) FROM normalized.station_snapshots)
    THEN
        RAISE EXCEPTION 'migrated row counts do not match the original tables';
    END IF;
END $$;

-- dbt's Bike Share views depend on the old tables; the next dbt build recreates them.
DROP TABLE normalized.observations, normalized.station_snapshots CASCADE;
ALTER TABLE normalized.observations_compact RENAME TO observations;
ALTER INDEX normalized.observations_compact_pkey RENAME TO observations_pkey;
CREATE INDEX observations_station ON normalized.observations (station_id, collection_key);

CREATE VIEW normalized.station_observations AS
SELECT c.collection_id, c.collection_key, c.collected_at, v.station_id,
       v.station_version_id, v.name, v.lat, v.lon, v.capacity,
       c.information_raw_id, c.status_raw_id, c.status_fetched_at,
       c.status_published_at AS source_published_at,
       o.station_reported_at, o.num_bikes_available, o.num_docks_available,
       o.num_bikes_disabled, o.num_docks_disabled,
       o.is_installed, o.is_renting, o.is_returning, true AS observed
FROM normalized.observations o
JOIN normalized.collections c USING (collection_key)
JOIN normalized.station_versions v USING (station_version_id)
UNION ALL
SELECT c.collection_id, c.collection_key, c.collected_at, v.station_id,
       v.station_version_id, v.name, v.lat, v.lon, v.capacity,
       c.information_raw_id, c.status_raw_id, c.status_fetched_at,
       c.status_published_at,
       NULL::timestamptz, NULL::integer, NULL::integer, NULL::integer, NULL::integer,
       NULL::boolean, NULL::boolean, NULL::boolean, false
FROM normalized.unobserved_stations u
JOIN normalized.collections c USING (collection_key)
JOIN normalized.station_versions v USING (station_version_id);
COMMENT ON VIEW normalized.station_observations IS
    'One row per station listed in a collection''s metadata, with its status if reported.';
