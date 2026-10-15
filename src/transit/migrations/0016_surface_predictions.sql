-- TTC's own arrival predictions for buses and streetcars (feed trips_surface,
-- gtfsrt.ttc.ca/trips/update). Only the newest snapshot's predictions are kept.

ALTER TABLE raw.ttc_realtime_snapshots DROP CONSTRAINT ttc_realtime_snapshots_feed_check;
ALTER TABLE raw.ttc_realtime_snapshots ADD CONSTRAINT ttc_realtime_snapshots_feed_check
    CHECK (feed IN ('trips_subway', 'alerts_subway', 'alerts_accessibility', 'vehicles',
                    'trips_surface'));

CREATE TABLE normalized.ttc_surface_predictions (
    stop_id text NOT NULL,
    trip_id text NOT NULL,
    route_id text NOT NULL,
    vehicle_id text,
    predicted_arrival timestamptz NOT NULL,
    snapshot_id bigint NOT NULL
);
CREATE INDEX ttc_surface_predictions_stop
    ON normalized.ttc_surface_predictions (stop_id, predicted_arrival);
