"""The weather map's current weather: city page summary and radar frames."""

import json
import struct
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
        "wind": {"speed": {"value": {"en": 18}}, "direction": {"value": {"en": "NE"}}},
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
    assert s["wind_dir"] == "NE"
    assert s["hourly"] == [{"at": "2026-11-12T18:00:00Z", "temperature": 7,
                            "condition": "Mainly cloudy", "precip_chance": 20,
                            "wind_kmh": None, "wind_dir": None}]  # fmt: skip
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


def tiff(values, width=2, height=2):
    """A minimal GeoTIFF as GeoMet's WCS returns it: one strip of float32 values."""
    tags = [
        (256, 3, 1, width),
        (257, 3, 1, height),
        (258, 3, 1, 32),
        (259, 3, 1, 1),
        (273, 4, 1, 8 + 2 + 12 * 7 + 4),
        (279, 4, 1, 4 * width * height),
        (339, 3, 1, 3),
    ]
    head = b"II*\x00" + struct.pack("<I", 8) + struct.pack("<H", len(tags))
    for tag, kind, count, value in tags:
        head += struct.pack("<HHII", tag, kind, count, value)
    return head + b"\x00" * 4 + struct.pack(f"<{width * height}f", *values)


def test_a_geotiff_grid_is_read_north_to_south():
    assert weather_now.read_tiff(tiff([1, 2, 3, 4])) == (2, 2, [1.0, 2.0, 3.0, 4.0])


def test_a_north_wind_blows_south():
    u, v = weather_now.wind_vector(10, 0)
    assert (round(u, 6), v) == (0, -10)
    u, v = weather_now.wind_vector(10, 270)
    assert (round(u, 6), round(v, 6)) == (10, 0)


def test_forecast_hours_start_with_this_hour_or_the_runs_first():
    run = datetime(2026, 11, 12, 12, tzinfo=UTC)
    hours = weather_now.forecast_hours(NOW, run)
    assert (hours[0].hour, len(hours)) == (17, weather_now.GRID_HOURS)
    assert weather_now.forecast_hours(datetime(2026, 11, 12, 12, 5, tzinfo=UTC), run)[0].hour == 13


PNG = b"\x89PNG\r\n\x1a\nfake"
RUN_CAPABILITIES = (
    '<Layer><Dimension name="reference_time" units="ISO8601" default="2026-11-12T12:00:00Z">'
    "2026-11-11T06:00:00Z/2026-11-12T12:00:00Z/PT6H</Dimension></Layer>"
)


class FakeClient:
    def __init__(self):
        self.grids = 0

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None, headers=None):
        if "citypageweather" in url:
            return json.dumps({"features": [{"properties": PAGE}]}).encode()
        if "GetCapabilities" in url:
            return (RUN_CAPABILITIES if "HRDPS" in url else CAPABILITIES).encode()
        if "WCS" in url:
            self.grids += 1
            hour = int(url.split("time=2026-11-12T")[1][:2])
            if "WSPD" in url:
                return tiff([5, 5, 5, 5])
            if "_WD" in url:
                return tiff([90, 90, 90, 90])
            return tiff([0, 0, 0, max(hour - 16, 0) * 1.5])  # Accumulated precipitation.
        return PNG


@pytest.mark.integration
def test_refresh_stores_the_page_and_new_frames_only(settings):
    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    url = settings.database_url
    first = weather_now.refresh(url, FakeClient(), NOW)
    assert first == {"status": "loaded", "frames": 2 * weather_now.FRAMES,
                     "grid_hours": weather_now.GRID_HOURS}  # fmt: skip
    again = FakeClient()
    assert weather_now.refresh(url, again, NOW) == {"status": "loaded", "frames": 0,
                                                    "grid_hours": 0}  # fmt: skip
    assert again.grids == 0  # The same run is not read again.
    with connect(url) as conn:
        found = weather_now.current(conn)
    assert found["page"]["temperature"] == 5.5
    assert len(found["radar"]["frames"]["rain"]) == weather_now.FRAMES
    grid = found["grid"]
    assert (grid["width"], len(grid["hours"])) == (2, weather_now.GRID_HOURS)
    first_hour = grid["hours"][0]  # 17:00, one hour after the precipitation began.
    assert (first_hour["u"][0], first_hour["v"][0]) == (-5.0, -0.0)
    assert first_hour["precip"] == [0, 0, 0, 1.5]
    client = TestClient(create_app(Settings(url), clock=lambda: NOW))
    body = client.get("/api/weather").json()["weather"]
    newest = body["radar"]["frames"]["snow"][-1]
    epoch = int(datetime.fromisoformat(newest).timestamp())
    image = client.get(f"/api/radar/snow/{epoch}.png")
    assert image.status_code == 200 and image.content == PNG
    assert "immutable" in image.headers["cache-control"]
    assert client.get("/api/radar/hail/1700000000.png").status_code == 404
    assert client.get(f"/api/radar/rain/{epoch + 60}.png").status_code == 404
