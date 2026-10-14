"""Environment Canada hourly weather: parsing a real response, revisions and refresh."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from transit import weather
from transit.db import connect
from transit.http import FetchError

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "weather" / "climate_hourly.json").read_text()
)
NOW = datetime(2026, 10, 10, 23, 0, tzinfo=UTC)


def test_real_response_parses_to_utc_hours():
    rows = weather.parse(FIXTURE)
    assert [r["observed_at"].hour for r in rows] == [19, 20, 21, 22]
    first = rows[0]
    assert first["observed_at"] == datetime(2026, 10, 10, 19, 0, tzinfo=UTC)
    assert (first["climate_id"], first["station_name"]) == ("6158355", "TORONTO CITY")
    assert (first["temp_c"], first["precip_mm"]) == (14.8, 0)
    assert first["weather"] is None  # "NA" in the source: not reported at this station.
    assert first["flags"] == {}


def test_hours_without_a_time_are_skipped_and_flags_kept():
    payload = copy.deepcopy(FIXTURE)
    payload["features"][0]["properties"]["UTC_DATE"] = None
    payload["features"][1]["properties"]["PRECIP_AMOUNT_FLAG"] = "T"
    rows = weather.parse(payload)
    assert len(rows) == 3
    assert rows[0]["flags"] == {"PRECIP_AMOUNT": "T"}
    with pytest.raises(FetchError):
        weather.parse({"type": "FeatureCollection"})


def test_a_truncated_backfill_fails_instead_of_dropping_hours():
    payload = {**FIXTURE, "numberMatched": 12_000}
    with pytest.raises(FetchError, match="4 of 12000 hours"):
        weather.parse(payload)


def test_query_filters_one_station_by_local_time():
    query = parse_qs(urlsplit(weather.url(NOW.replace(hour=4), NOW)).query)
    assert query["CLIMATE_IDENTIFIER"] == ["6158355"]
    assert query["datetime"] == ["2026-10-10T00:00:00Z/2026-10-10T19:00:00Z"]


class Client:
    def __init__(self, payload=FIXTURE, fail=False):
        self.payload, self.fail, self.urls = payload, fail, []

    def fetch_bytes(self, url, *, host, max_bytes=None):
        assert host == weather.HOST
        self.urls.append(url)
        if self.fail:
            raise FetchError("HTTP 503: retries exhausted")
        return json.dumps(self.payload).encode()


@pytest.mark.integration
def test_refresh_overwrites_revised_hours_and_records_attempts(settings):
    first = weather.refresh(settings.database_url, Client(), NOW)
    assert (first["status"], first["hours"]) == ("loaded", 4)
    revised = copy.deepcopy(FIXTURE)
    revised["features"][0]["properties"]["PRECIP_AMOUNT"] = 1.2
    client = Client(revised)
    weather.refresh(settings.database_url, client, NOW)
    # The next refresh starts two days before the latest stored hour.
    since = parse_qs(urlsplit(client.urls[0]).query)["datetime"][0].split("/")[0]
    assert since == "2026-10-08T18:00:00Z"
    assert weather.refresh(settings.database_url, Client(fail=True), NOW)["status"] == "failed"
    with connect(settings.database_url) as conn:
        rows = conn.execute(
            "SELECT observed_at, precip_mm FROM normalized.weather_hourly ORDER BY observed_at"
        ).fetchall()
        assert len(rows) == 4 and float(rows[0]["precip_mm"]) == 1.2
        statuses = [
            r["status"]
            for r in conn.execute("SELECT status FROM ops.weather_refreshes ORDER BY refresh_id")
        ]
        assert statuses == ["loaded", "loaded", "failed"]
