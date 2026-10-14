-- Run once on the collector server. Publishes every table the collectors write; the
-- development machine subscribes to it (docs/collector.md). Raw TTC snapshots stay on the
-- server: they are kept for replay only, and replicating them would move about 1 GB a day.

-- Without a primary key, a published table needs a replica identity for its retention
-- deletes; the subscriber finds rows through the passed_at index.
ALTER TABLE normalized.ttc_surface_passages REPLICA IDENTITY FULL;

CREATE PUBLICATION collected FOR TABLE
    -- Bike Share availability
    raw.feed_payloads,
    raw.payload_bodies,
    normalized.collections,
    normalized.observations,
    normalized.station_versions,
    normalized.unobserved_stations,
    ops.ingestion_attempts,
    ops.ingestion_runs,
    ops.quality_checks,
    -- TTC schedule, realtime and derived events
    normalized.ttc_gtfs_versions,
    normalized.ttc_calendar,
    normalized.ttc_calendar_dates,
    normalized.ttc_routes,
    normalized.ttc_stops,
    normalized.ttc_trips,
    normalized.ttc_stop_times,
    normalized.ttc_line_stops,
    normalized.ttc_surface_routes,
    normalized.ttc_surface_stops,
    normalized.ttc_surface_patterns,
    normalized.ttc_surface_pattern_stops,
    normalized.ttc_surface_pattern_starts,
    normalized.ttc_alerts,
    normalized.ttc_alert_versions,
    normalized.ttc_current_predictions,
    normalized.ttc_train_stop_events,
    normalized.ttc_vehicles,
    normalized.ttc_surface_passages,
    ops.ttc_feed_state,
    ops.ttc_poll_runs,
    ops.ttc_record_issues,
    ops.ttc_static_refreshes,
    ops.ttc_vehicle_progress,
    ops.source_backoff,
    -- City road restrictions
    raw.road_restriction_responses,
    normalized.road_restrictions,
    normalized.road_restriction_routes,
    normalized.road_restriction_checks,
    ops.road_restriction_refreshes;
