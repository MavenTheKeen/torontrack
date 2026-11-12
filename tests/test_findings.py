"""Findings computed from stored data: checked on synthetic weather and Bike Share days."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from transit import findings
from transit.db import connect

TORONTO = ZoneInfo("America/Toronto")
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def seed(conn, days: int = 120) -> None:
    """Alternate cold (high 2 °C) and warm (high 27 °C) days; every other pair is wet. Wet
    days have exactly 25% fewer trips than dry days at the same temperature."""
    response = conn.execute(
        """INSERT INTO raw.weather_responses (fetched_at, url, payload)
           VALUES (now(), 'test', '{}') RETURNING response_id"""
    ).fetchone()["response_id"]
    start = date(2025, 1, 1)
    with conn.cursor() as cur:
        for i in range(days):
            day = start + timedelta(days=i)
            temp = 2 if i % 2 else 27
            wet = (i // 2) % 2 == 1
            midnight = datetime(day.year, day.month, day.day, tzinfo=TORONTO)
            for hour in range(24):
                cur.execute(
                    """INSERT INTO normalized.weather_hourly (climate_id, observed_at,
                           station_name, temp_c, precip_mm, response_id)
                       VALUES ('6158355', %s, 'TORONTO CITY', %s, %s, %s)""",
                    (midnight + timedelta(hours=hour), temp, 0.1 if wet else 0, response),
                )
            trips = (1000 if temp == 2 else 3000) * (0.75 if wet else 1)
            cur.execute(
                """INSERT INTO normalized.bikeshare_trip_station_days (trip_date, station_id,
                       departures, arrivals, member_departures, ebike_departures, round_trips)
                   VALUES (%s, '1', %s, 0, 0, 0, 0)""",
                (day, int(trips)),
            )


@pytest.mark.integration
def test_bikeshare_rain_compares_like_with_like(settings):
    with connect(settings.database_url) as conn:
        conn.execute("TRUNCATE normalized.weather_hourly, raw.weather_responses CASCADE")
        conn.execute("TRUNCATE normalized.bikeshare_trip_station_days")
        seed(conn)
        found = findings.bikeshare_rain(conn)
        assert found["title"] == "A wet day costs Bike Share about 25% of its trips"
        assert "3.0 times the trips" in found["summary"]
        dry, wet = found["chart"]["series"]
        assert found["chart"]["x"][0] == "0 to 5" and dry["values"][0] == 1000
        assert wet["values"][-1] == 2250
        # No delay log, 311 or inspections: those findings are left out, not shown empty.
        listed = findings.all_findings(conn, NOW.date())
    assert [f["id"] for f in listed] == ["bikeshare-weather"]

    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    client = TestClient(create_app(Settings(settings.database_url), clock=lambda: NOW))
    body = client.get("/api/findings").json()
    assert [f["id"] for f in body["findings"]] == ["bikeshare-weather"]
