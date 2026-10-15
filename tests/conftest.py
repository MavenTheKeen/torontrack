import os

import pytest

from transit.config import Settings
from transit.db import connect, init_db


@pytest.fixture
def settings():
    """Only reset project tables in an explicitly disposable database."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration checks")
    with connect(url) as conn:
        database = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if not database.endswith("_test"):
            pytest.fail("Integration database name must end in _test; tables are cleared")
    init_db(url)
    with connect(url) as conn:
        # An interrupted rollback test can leave its failure-injection trigger behind.
        conn.execute("DROP TRIGGER IF EXISTS reject_test_write ON normalized.observations")
        conn.execute("TRUNCATE ops.ingestion_runs CASCADE")
        conn.execute("TRUNCATE raw.payload_bodies, normalized.station_versions CASCADE")
        conn.execute("TRUNCATE raw.ttc_delay_files, ops.ttc_delay_refreshes CASCADE")
        conn.execute("TRUNCATE ops.source_backoff, ops.transformation_runs")
        conn.execute("TRUNCATE ops.alert_state, ops.alerts")
        conn.execute(
            "TRUNCATE normalized.ttc_vehicles, normalized.ttc_surface_passages, "
            "ops.ttc_vehicle_progress"
        )
        conn.execute("TRUNCATE normalized.regional_versions, ops.regional_refreshes CASCADE")
        conn.execute("TRUNCATE normalized.city_events, ops.city_event_refreshes CASCADE")
        conn.execute("TRUNCATE normalized.ticketed_events, ops.ticketed_event_refreshes")
        conn.execute(
            """TRUNCATE normalized.agency_versions, normalized.agency_vehicles,
               ops.agency_refreshes, ops.agency_polls CASCADE"""
        )
        conn.execute(
            """TRUNCATE normalized.bikeshare_trip_files, normalized.bikeshare_trip_station_days,
               normalized.bikeshare_trip_station_hours, normalized.bikeshare_trip_pairs,
               normalized.bikeshare_trip_stations, ops.bikeshare_trip_refreshes"""
        )
        conn.execute(
            """TRUNCATE raw.road_restriction_responses, normalized.road_restrictions,
               ops.road_restriction_refreshes, normalized.road_restriction_checks,
               normalized.road_restriction_routes"""
        )
        conn.execute(
            "TRUNCATE raw.weather_responses, normalized.weather_hourly, ops.weather_refreshes"
        )
        conn.execute(
            """TRUNCATE normalized.ttc_gtfs_versions, raw.ttc_realtime_snapshots,
               normalized.ttc_train_stop_events, normalized.ttc_current_predictions,
               normalized.ttc_alerts, ops.ttc_poll_runs, ops.ttc_feed_state,
               ops.ttc_static_refreshes CASCADE"""
        )
        # Incremental dbt models keep history between builds by design; tests start empty.
        for table in (
            "analytics.ttc_headways",
            "analytics.ttc_scheduled_service",
            "analytics.ttc_service_days",
            "analytics.ttc_collection_gaps",
            "analytics.ttc_surface_scheduled",
            "analytics.ttc_surface_headways",
        ):
            if conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"]:
                conn.execute(f"TRUNCATE {table}")
    return Settings(url)
