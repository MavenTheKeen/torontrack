"""ECCC Air Quality Health Index for Toronto: reading, loading and the current reading."""

import json
from datetime import UTC, datetime

import pytest

from transit import air_quality
from transit.db import connect

NOW = datetime(2026, 11, 12, 17, 0, tzinfo=UTC)


def obs(location, name, at, aqhi):
    return {"location_id": location, "location_name_en": name,
            "observation_datetime": at, "aqhi": aqhi}  # fmt: skip


def fcst(location, published, at, aqhi):
    return {"location_id": location, "publication_datetime": published,
            "forecast_datetime": at, "aqhi": aqhi}  # fmt: skip


OBSERVATIONS = [
    obs("FCWYG", "Toronto Downtown", "2026-11-12T15:00:00Z", 2.09),
    obs("FCWYG", "Toronto Downtown", "2026-11-12T16:00:00Z", 3.6),
    obs("FDQBU", "Toronto East", "2026-11-12T16:00:00Z", 1.26),
    obs("FDQBU", "Toronto East", "2026-11-12T17:00:00Z", None),
]
FORECASTS = [
    fcst("FCWYG", "2026-11-12T11:00:00Z", "2026-11-12T20:00:00Z", 4),
    fcst("FCWYG", "2026-11-12T11:00:00Z", "2026-11-13T20:00:00Z", 7),
    fcst("FCWYG", "2026-11-11T23:00:00Z", "2026-11-12T20:00:00Z", 2),  # Older publication.
]


def test_risk_categories_follow_eccc():
    assert [air_quality.risk(v) for v in (1, 3.4, 3.6, 6, 7, 10, 11)] == [
        "Low", "Low", "Moderate", "Moderate", "High", "High", "Very high",
    ]  # fmt: skip


def test_only_the_newest_forecast_is_kept():
    assert [f[2].day for f in air_quality.read_forecasts(FORECASTS)] == [12, 13]
    assert len(air_quality.read_observations(OBSERVATIONS)) == 3


class FakeClient:
    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None, headers=None):
        assert host == air_quality.HOST and "bbox=" in url
        items = OBSERVATIONS if "observations" in url else FORECASTS
        return json.dumps({"features": [{"properties": i} for i in items]}).encode()


@pytest.mark.integration
def test_refresh_and_current(settings):
    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    url = settings.database_url
    assert air_quality.refresh(url, FakeClient(), NOW) == {
        "status": "loaded",
        "observations": 3,
        "forecasts": 2,
    }
    air_quality.refresh(url, FakeClient(), NOW)  # Idempotent.
    with connect(url) as conn:
        found = air_quality.current(conn, NOW)
    assert [(s["name"], s["aqhi"], s["risk"]) for s in found["stations"]] == [
        ("Toronto Downtown", 3.6, "Moderate"),
        ("Toronto East", 1.26, "Low"),
    ]
    assert [f["aqhi"] for f in found["forecast"]] == [4.0, 7.0]
    client = TestClient(create_app(Settings(url), clock=lambda: NOW))
    assert client.get("/api/air").json()["air"]["stations"][0]["id"] == "FCWYG"
