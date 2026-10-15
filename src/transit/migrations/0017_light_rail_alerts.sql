-- Service alerts for the light rail Lines 5 and 6, which TTC publishes only in its
-- combined alerts feed (alerts_light_rail keeps the alerts that name them).

ALTER TABLE raw.ttc_realtime_snapshots DROP CONSTRAINT ttc_realtime_snapshots_feed_check;
ALTER TABLE raw.ttc_realtime_snapshots ADD CONSTRAINT ttc_realtime_snapshots_feed_check
    CHECK (feed IN ('trips_subway', 'alerts_subway', 'alerts_accessibility', 'vehicles',
                    'trips_surface', 'alerts_light_rail'));
