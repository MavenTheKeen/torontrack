"""The weather map's current weather: city page summary and radar frames."""

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from transit import weather_now
from transit.db import connect

NOW = datetime(2026, 11, 12, 17, 30, tzinfo=UTC)
CAPABILITIES = (
    '<Layer><Dimension name="time" units="ISO8601" default="2026-11-12T17:24:00Z">'
    "2026-11-12T14:24:00Z/2026-11-12T17:24:00Z/PT6M</Dimension></Layer>"
)
PAGE = {
    "lastUpdated": "2026-11-12T17:02:46Z",
    "currentConditions": {
        "timestamp": {"en": "2026-11-12T17:00:00Z"},
        "condition": {"en": "Mostly Cloudy"},
        "temperature": {"value": {"en": 5.5}},
        "windChill": {"value": {"en": 1}},
        "wind": {"speed": {"value": {"en": 18}}},
        "relativeHumidity": {"value": {"en": 68}},
    },
    "hourlyForecastGroup": {"hourlyForecasts": [
        {"timestamp": "2026-11-12T18:00:00Z", "temperature": {"value": {"en": 7}},
         "condition": {"en": "Mainly cloudy"}, "lop": {"value": {"en": 20}}},
    ]},
    "forecastGroup": {"forecasts": [
        {"period": {"textForecastName": {"en": "Today"}}, "textSummary": {"en": "High 8."},
         "temperatures": {"temperature": [{"value": {"en": 8}}]},
         "precipitation": {"precipPeriods": [{"value": {"en": "rain"}}]}},
    ]},
    "warnings": [],
}  # fmt: skip


def test_the_last_frames_of_the_time_dimension():
    times = weather_now.latest_times(CAPABILITIES, count=3)
    assert [t.strftime("%H:%M") for t in times] == ["17:12", "17:18", "17:24"]


def test_the_city_page_is_summarized_in_plain_values():
    s = weather_now.summarize(PAGE)
    assert (s["condition"], s["temperature"], s["feels_like"], s["wind_kmh"]) == (
        "Mostly Cloudy", 5.5, 1, 18,
    )  # fmt: skip
    assert s["hourly"] == [{"at": "2026-11-12T18:00:00Z", "temperature": 7,
                            "condition": "Mainly cloudy", "precip_chance": 20}]  # fmt: skip
    assert s["daily"][0] == {"name": "Today", "summary": "High 8.", "temperature": 8,
                             "precip": ["rain"]}  # fmt: skip


def test_radar_requests_cover_the_toronto_area_at_the_frame_time():
    url = urlsplit(
        weather_now.frame_url("RADAR_1KM_RRAI", datetime(2026, 11, 12, 17, 24, tzinfo=UTC))
    )
    query = parse_qs(url.query)
    assert url.hostname == weather_now.WMS_HOST and query["crs"] == ["EPSG:3857"]
    assert query["time"] == ["2026-11-12T17:24:00Z"]
    width, height = weather_now.image_size()
    assert (width, 650 < height < 750) == (800, True)


PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeClient:
    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None, headers=None):
        if "citypageweather" in url:
            return json.dumps({"features": [{"properties": PAGE}]}).encode()
        if "GetCapabilities" in url:
            return CAPABILITIES.encode()
        return PNG


@pytest.mark.integration
def test_refresh_stores_the_page_and_new_frames_only(settings):
    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    url = settings.database_url
    first = weather_now.refresh(url, FakeClient(), NOW)
    assert first == {"status": "loaded", "frames": 2 * weather_now.FRAMES}
    assert weather_now.refresh(url, FakeClient(), NOW)["frames"] == 0
    with connect(url) as conn:
        found = weather_now.current(conn)
    assert found["page"]["temperature"] == 5.5
    assert len(found["radar"]["frames"]["rain"]) == weather_now.FRAMES
    client = TestClient(create_app(Settings(url), clock=lambda: NOW))
    body = client.get("/api/weather").json()["weather"]
    newest = body["radar"]["frames"]["snow"][-1]
    epoch = int(datetime.fromisoformat(newest).timestamp())
    image = client.get(f"/api/radar/snow/{epoch}.png")
    assert image.status_code == 200 and image.content == PNG
    assert "immutable" in image.headers["cache-control"]
    assert client.get("/api/radar/hail/1700000000.png").status_code == 404
    assert client.get(f"/api/radar/rain/{epoch + 60}.png").status_code == 404
