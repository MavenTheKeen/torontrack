-- Neighbouring agencies' own arrival predictions (GTFS-Realtime trip updates); only the
-- latest poll's predictions per agency are kept.

CREATE TABLE normalized.agency_predictions (
    agency text NOT NULL,
    stop_id text NOT NULL,
    trip_id text NOT NULL,
    route_id text NOT NULL,
    vehicle_id text,
    predicted_arrival timestamptz NOT NULL
);
CREATE INDEX agency_predictions_stop
    ON normalized.agency_predictions (agency, stop_id, predicted_arrival);

ALTER TABLE ops.agency_polls
    ADD COLUMN predictions_at timestamptz,
    ADD COLUMN predictions integer,
    ADD COLUMN predictions_error text;
