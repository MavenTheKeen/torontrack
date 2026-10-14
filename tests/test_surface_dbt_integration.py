"""Run the real dbt project on controlled bus passages and check the surface models."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_ttc_dbt_integration import SIX_AM, arrive
from tests.ttc_helpers import T0, static_zip
from transit.db import connect
from transit.transform import run_transform
from transit.ttc import static_gtfs, surface
from transit.web import report

pytestmark = pytest.mark.integration

# 07:10 Toronto on Friday 2026-10-09.
SEVEN_TEN = datetime(2026, 10, 9, 11, 10, tzinfo=UTC)


def test_surface_headways_bunching_and_gaps(settings, monkeypatch):
    executable = os.environ.get("DBT_EXECUTABLE")
    if not executable:
        pytest.skip("Set DBT_EXECUTABLE to the isolated dbt executable")
    with connect(settings.database_url) as conn:
        feed = static_gtfs.parse(static_zip())
        static_gtfs.load(conn, feed, "test://gtfs", T0)
        surface.load(conn, surface.parse(static_zip()), feed.feed_version)
        # Make the eastbound 10 frequent: 12 trips starting in hour 7, so one every 5
        # minutes at Midland Ave (663, 3 minutes along) in service hour 7.
        conn.execute(
            """UPDATE normalized.ttc_surface_pattern_starts s SET trips = 12
               FROM normalized.ttc_surface_patterns p
               WHERE p.feed_version = s.feed_version AND p.pattern_id = s.pattern_id
                 AND p.route_id = '10' AND p.direction_id = 0 AND s.start_hour = 7"""
        )
        arrive(conn, "101", SIX_AM)  # A subway arrival makes the date a service day.
        passages = [
            (0, "b1"),
            (300, "b2"),  # On schedule.
            (330, "b3"),  # 30 s behind b2: bunched.
            (330 + 870, "b4"),  # A long gap.
            (330 + 900, "b4"),  # The same bus again: not a headway.
        ]
        for seconds, vehicle in passages:
            conn.execute(
                """INSERT INTO normalized.ttc_surface_passages
                   (passed_at, route_id, direction_id, stop_id, vehicle_id, pattern_id,
                    report_gap_seconds) VALUES (%s, '10', 0, '663', %s, 1, 30)""",
                (SEVEN_TEN + timedelta(seconds=seconds), vehicle),
            )
    assert run_transform(settings.database_url, executable, "dbt")["status"] == "succeeded"
    with connect(settings.database_url) as conn:
        headways = conn.execute(
            """SELECT headway_seconds, scheduled_headway_seconds, same_vehicle
               FROM analytics.ttc_surface_headways ORDER BY passed_at"""
        ).fetchall()
        assert [(h["headway_seconds"], h["same_vehicle"]) for h in headways] == [
            (300, False),
            (30, False),
            (870, False),
            (30, True),
        ]
        assert {float(h["scheduled_headway_seconds"]) for h in headways} == {300.0}
        [day] = conn.execute("SELECT * FROM analytics.ttc_surface_reliability_daily").fetchall()
    assert (day["route_id"], day["direction_id"], day["day_type"]) == ("10", 0, "weekday")
    assert (day["headways"], day["bunched"], day["long_gaps"]) == (3, 1, 1)
    assert float(day["scheduled_headway_seconds"]) == 300.0

    monkeypatch.setattr(report, "ROUTE_MIN_HEADWAYS", 1)
    with connect(settings.database_url) as conn:
        summary = report.surface(conn)
    assert (summary["headways"], summary["bunched_pct"], summary["long_pct"]) == (3, 33.3, 33.3)
    # Waits: (300^2 + 30^2 + 870^2) / (2 * 1200) = 353 s against 150 s scheduled.
    assert (summary["wait_seconds"], summary["scheduled_wait_seconds"]) == (353, 150)
    [route] = summary["routes_ranked"]
    assert (route["short_name"], route["mode"], route["ewt_seconds"]) == ("10", "bus", 203)


def test_weekdays_with_and_without_a_road_restriction(settings, monkeypatch):
    executable = os.environ.get("DBT_EXECUTABLE")
    if not executable:
        pytest.skip("Set DBT_EXECUTABLE to the isolated dbt executable")
    tuesday = SEVEN_TEN + timedelta(days=4)  # 07:10 on Tuesday 2026-10-13.
    with connect(settings.database_url) as conn:
        feed = static_gtfs.parse(static_zip())
        static_gtfs.load(conn, feed, "test://gtfs", T0)
        surface.load(conn, surface.parse(static_zip()), feed.feed_version)
        conn.execute(
            """UPDATE normalized.ttc_surface_pattern_starts s SET trips = 12
               FROM normalized.ttc_surface_patterns p
               WHERE p.feed_version = s.feed_version AND p.pattern_id = s.pattern_id
                 AND p.route_id = '10' AND p.direction_id = 0 AND s.start_hour = 7"""
        )
        # Friday is bunched and has a closure along the route; Tuesday runs to schedule.
        for day, offsets in ((SEVEN_TEN, [0, 30, 600, 630]), (tuesday, [0, 300, 600, 900])):
            arrive(conn, f"1{day.day}", day - timedelta(hours=1))
            for n, seconds in enumerate(offsets):
                conn.execute(
                    """INSERT INTO normalized.ttc_surface_passages
                       (passed_at, route_id, direction_id, stop_id, vehicle_id, pattern_id,
                        report_gap_seconds) VALUES (%s, '10', 0, '663', %s, 1, 30)""",
                    (day + timedelta(seconds=seconds), f"b{n}"),
                )
        conn.execute(
            """INSERT INTO normalized.road_restrictions (restriction_id, kind, name, max_impact,
                   work_period, starts_at, ends_at, path, first_seen_at, last_seen_at,
                   response_id)
               VALUES ('d1', 'closure', 'Danforth Rd', 'High', 'Continuous', %s, %s,
                       '[[-79.268, 43.7], [-79.262, 43.7]]', %s, %s, 1)""",
            (SEVEN_TEN - timedelta(days=1), SEVEN_TEN + timedelta(days=1), T0, T0),
        )
        conn.execute(
            """INSERT INTO normalized.road_restriction_routes VALUES
               ('d1', %s, '10', 0, 480)""",
            (feed.feed_version,),
        )
    assert run_transform(settings.database_url, executable, "dbt")["status"] == "succeeded"
    with connect(settings.database_url) as conn:
        [closed] = conn.execute("SELECT * FROM analytics.ttc_surface_closure_days").fetchall()
        assert (str(closed["service_date"]), closed["closures"], closed["high_impact"]) == (
            "2026-10-09",
            1,
            1,
        )
        monkeypatch.setattr(report, "CLOSURE_MIN_DAYS", 1)
        monkeypatch.setattr(report, "ROUTE_MIN_HEADWAYS", 1)
        closures = report.surface(conn)["closures"]
    assert (closures["with"]["route_days"], closures["without"]["route_days"]) == (1, 1)
    assert closures["with"]["bunched_pct"] == 66.7 and closures["without"]["bunched_pct"] == 0
    assert closures["with"]["ewt_seconds"] > closures["without"]["ewt_seconds"] == 0
