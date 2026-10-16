"""Run the real dbt project on controlled TTC arrivals and check the reliability models."""

import copy
import os
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_weather import FIXTURE as WEATHER
from tests.ttc_helpers import T0, alert_feed, static_zip, trip_feed
from transit import weather
from transit.db import connect
from transit.transform import run_transform
from transit.ttc import static_gtfs, store
from transit.web import report

pytestmark = pytest.mark.integration

# 06:00 Toronto on Friday 2026-10-09; the helper schedule has one southbound train per
# hour at Wellesley (13806), so the scheduled headway in service hour 6 is 3600 s.
SIX_AM = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


def arrive(conn, train, at, stop="13806"):
    """A snapshot listing the train as due, then one where the platform is gone."""
    for when, stops in (
        (at - timedelta(seconds=10), [(stop, 3, 10)]),
        (at + timedelta(seconds=15), []),
    ):
        trips = [{"trip_id": f"t{train}", "vehicle": train, "route_id": "1", "stops": stops}]
        snapshot_id, _ = store.store_snapshot(
            conn, "trips_subway", "test://", trip_feed(when, trips if stops else []), when
        )
        store.normalize_snapshot(conn, snapshot_id)


def log_incidents(conn, incidents):
    """Rows as TTC's official delay log would provide them (see transit.ttc.delays)."""
    conn.execute(
        """INSERT INTO raw.ttc_delay_files VALUES (repeat('a', 64), now(), 'test://d',
               'test://c', ''::bytea, ''::bytea)"""
    )
    conn.execute(
        """INSERT INTO normalized.ttc_delay_codes
           VALUES ('MUSAN', 'UNSANITARY VEHICLE', repeat('a', 64))"""
    )
    for source_id, at, bound, gap in incidents:
        conn.execute(
            """INSERT INTO normalized.ttc_official_delays (source_id, delay_at, min_delay,
                   min_gap, station_text, station_key, station_match, line_text, route_id,
                   bound, code, file_sha256)
               VALUES (%s, %s, 5, %s, 'WELLESLEY STATION', 'wellesley', 'exact', 'YU', '1',
                       %s, 'MUSAN', repeat('a', 64))""",
            (source_id, at, gap, bound),
        )


def test_ttc_reliability_models(settings):
    executable = os.environ.get("DBT_EXECUTABLE")
    if not executable:
        pytest.skip("Set DBT_EXECUTABLE to the isolated dbt executable")
    with connect(settings.database_url) as conn:
        static_gtfs.load(conn, static_gtfs.parse(static_zip()), "test://gtfs", T0)
        # W is a work train at 05:50, before Wellesley's first scheduled train (06:06).
        arrive(conn, "W", SIX_AM - timedelta(minutes=10))
        for minutes in range(9, 0, -1):  # Empty polls: the collector kept running.
            when = SIX_AM - timedelta(minutes=minutes)
            snapshot_id, _ = store.store_snapshot(
                conn, "trips_subway", "test://", trip_feed(when, []), when
            )
            store.normalize_snapshot(conn, snapshot_id)
        for train, offset in (("A", 0), ("B", 180), ("C", 210), ("D", 900)):
            arrive(conn, train, SIX_AM + timedelta(seconds=offset))
        outage = alert_feed(
            T0,
            [
                {
                    "id": "9",
                    "effect": "ACCESSIBILITY_ISSUE",
                    "stops": ["13864"],
                    "text": "Bloor-Yonge: Elevator 1A out of service",
                }
            ],
        )
        snapshot_id, _ = store.store_snapshot(conn, "alerts_accessibility", "test://", outage, T0)
        store.normalize_snapshot(conn, snapshot_id)
        # Rain recorded only in the 20:00 UTC observation.
        wet = copy.deepcopy(WEATHER)
        wet["features"][1]["properties"]["PRECIP_AMOUNT"] = 0.4
        weather.load(conn, wet, "test://weather", T0)
        log_incidents(
            conn,
            [
                # During the 11.5-minute southbound gap before train D.
                (1, SIX_AM + timedelta(seconds=300), "Southbound", 10),
                # Northbound: nothing was observed in that direction.
                (2, SIX_AM + timedelta(seconds=300), "Northbound", 8),
                # The day before collection began: cannot be checked, so not listed.
                (3, SIX_AM - timedelta(days=1), "Southbound", 10),
            ],
        )

    assert run_transform(settings.database_url, executable, "dbt")["status"] == "succeeded"

    with connect(settings.database_url) as conn:
        headways = conn.execute(
            """SELECT train_id, headway_seconds, implausible, outside_service,
                      spans_collection_gap, service_hour
               FROM analytics.ttc_headways ORDER BY arrived_at"""
        ).fetchall()
        assert [
            (h["train_id"], h["headway_seconds"], h["implausible"], h["outside_service"])
            for h in headways
        ] == [
            ("W", None, False, False),
            ("A", 600, False, True),  # Gap after the work train: not a delay, excluded.
            ("B", 180, False, False),
            ("C", 30, True, False),  # Two trains 30 s apart: excluded from metrics.
            ("D", 690, False, False),
        ]
        assert [h["service_hour"] for h in headways] == [5, 6, 6, 6, 6]
        assert not any(h["spans_collection_gap"] for h in headways)
        hourly = conn.execute("SELECT * FROM analytics.ttc_headway_reliability_hourly").fetchall()
        assert len(hourly) == 1
        row = hourly[0]
        assert (row["route_id"], row["direction_id"], row["service_hour"]) == ("1", 0, 6)
        assert (row["observed_headways"], row["compared_headways"]) == (2, 2)
        assert float(row["scheduled_headway_seconds"]) == 3600
        assert row["median_headway_seconds"] == 435
        assert (row["regular_headways"], row["long_gaps"]) == (2, 0)
        # Excess wait time sums over B (180 s) and D (690 s), each scheduled 3600 s apart.
        assert (
            row["compared_seconds"],
            row["compared_squared_seconds"],
            row["compared_scheduled_product"],
        ) == (870, 180**2 + 690**2, 870 * 3600)
        assert (row["day_type"], row["conditions"]) == ("weekday", None)  # No weather yet.
        stations = conn.execute(
            "SELECT station_key, compared_headways FROM analytics.ttc_station_reliability"
        ).fetchall()
        assert [(s["station_key"], s["compared_headways"]) for s in stations] == [("wellesley", 2)]
        # The report combines those sums: a random rider waited 292 s on average, against
        # 1800 s promised by an hourly schedule, so extra wait is negative.
        summary = report.report(conn)
        assert summary["coverage"]["weekdays"] == 1
        (line,) = summary["lines"]
        assert (line["route_id"], line["days"], line["gaps"]) == ("1", "weekday", 2)
        assert (line["wait_seconds"], line["scheduled_wait_seconds"]) == (292, 1800)
        assert line["ewt_seconds"] == round((180**2 + 690**2 - 870 * 3600) / (2 * 870))
        assert [p["period"] for p in summary["periods"]] == ["early"]
        assert summary["stations"] == []  # Fewer measured gaps than a ranking needs.
        # Both gaps are well within the hourly schedule, so neither is a long gap.
        assert (
            conn.execute("SELECT count(*) AS n FROM analytics.ttc_long_gaps").fetchone()["n"] == 0
        )
        outage_row = conn.execute("SELECT * FROM analytics.ttc_elevator_outages").fetchone()
        assert (outage_row["station_name"], outage_row["device_type"]) == (
            "Bloor-Yonge",
            "elevator",
        )
        assert outage_row["active"] is True and outage_row["began_before_collection"] is True
        detection = {
            r["source_id"]: r
            for r in conn.execute("SELECT * FROM analytics.ttc_delay_detection").fetchall()
        }
        assert set(detection) == {1, 2}
        assert detection[1]["detected"] is True and detection[1]["observed_max_gap_seconds"] == 690
        assert detection[1]["code_description"] == "UNSANITARY VEHICLE"
        assert detection[2]["detected"] is False and detection[2]["observed_headways"] == 0
        assert all(r["in_coverage"] for r in detection.values())
        # 19:00 UTC is 15:00 in Toronto (daylight time). ECCC does not say whether the
        # 20:00 observation's rain fell before or after it, so both hours it bounds are wet.
        hours = conn.execute(
            "SELECT * FROM staging.stg_weather_hourly ORDER BY observed_at"
        ).fetchall()
        assert [(str(h["service_date"]), h["service_hour"], h["conditions"]) for h in hours] == [
            ("2026-10-10", 15, "rain"),
            ("2026-10-10", 16, "rain"),
            ("2026-10-10", 17, "dry"),
            ("2026-10-10", 18, "dry"),
        ]
        assert float(hours[0]["temp_c"]) == 14.8  # Mean of 14.8 and 14.7, rounded.

        # Days and outages outlive the train visits and poll runs they came from.
        conn.execute(
            "INSERT INTO analytics.ttc_service_days VALUES ('2026-01-05', 'old', 'weekday')"
        )
        conn.execute(
            """INSERT INTO analytics.ttc_collection_gaps
               VALUES ('2026-01-05 12:00+00', '2026-01-05 14:00+00', 7200)"""
        )

        # Incremental: a later run keeps earlier rows and adds new ones exactly once.
        arrive(conn, "E", SIX_AM + timedelta(seconds=1200))
    assert run_transform(settings.database_url, executable, "dbt")["status"] == "succeeded"
    with connect(settings.database_url) as conn:
        rows = conn.execute(
            "SELECT train_id, headway_seconds FROM analytics.ttc_headways ORDER BY arrived_at"
        ).fetchall()
        assert [(r["train_id"], r["headway_seconds"]) for r in rows][-2:] == [
            ("D", 690),
            ("E", 300),
        ]
        assert len(rows) == 6
        assert conn.execute(
            "SELECT day_type FROM analytics.ttc_service_days WHERE service_date = '2026-01-05'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM analytics.ttc_collection_gaps WHERE gap_seconds = 7200"
        ).fetchone()

    # A full refresh (after a column change) rebuilds the same rows from retained visits.
    assert (
        run_transform(settings.database_url, executable, "dbt", full_refresh=True)["status"]
        == "succeeded"
    )
    with connect(settings.database_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM analytics.ttc_headways").fetchone()["n"] == 6
