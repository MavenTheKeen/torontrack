-- Expose the observation's own station_id in station_observations, so a filter on one
-- station reaches observations_station instead of scanning every observation. The two
-- are equal by construction (a station version belongs to one station).
CREATE OR REPLACE VIEW normalized.station_observations AS
SELECT c.collection_id, c.collection_key, c.collected_at, o.station_id,
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
