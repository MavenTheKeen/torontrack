"""City of Toronto events: streaming the feed, merging its copies, loading and the API."""

import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from transit import events
from transit.config import Settings
from transit.db import connect
from transit.web.app import create_app

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def record(rid: str, name="Harvest Market", days=(0, 1), **overrides) -> dict:
    base = {
        "id": rid,
        "event_name": name,
        "event_status": "Approved",
        "event_category": ["Farmers' Market"],
        "featured_event": "Yes",
        "free_event": "Yes",
        "event_website": "example.org/market",
        "__ModifiedOn": "2026-10-01T09:00:00-04:00",
        "event_dates": [{"date": ms(NOW + timedelta(days=d))} for d in days],
        "event_locations": [
            {
                "location_name": "Nathan Phillips Square",
                "location_address": "100 Queen St W",
                "location_gps": '[{"gps_lat":43.6525,"gps_lng":-79.3835}]',
            }
        ],
    }
    return {**base, **overrides}


def feed(records) -> io.BytesIO:
    return io.BytesIO(json.dumps({"value": records}).encode())


def test_copies_of_one_event_are_merged_and_unusable_records_skipped():
    rows, skipped = events.read(
        feed(
            [
                record("a", days=(0, 1)),
                record("b", days=(1, 2)),  # Another copy of the same market.
                record("c", name="Gallery Talk", days=(90,)),  # Beyond the window.
                record("d", name="Draft", event_status="Pending"),
                record("e", name="Nowhere", event_locations=[{"location_gps": ""}]),
            ]
        ),
        NOW,
    )
    [market] = rows
    assert [d - NOW for d in market["dates"]] == [timedelta(days=d) for d in (0, 1, 2)]
    assert market["website"] == "https://example.org/market" and market["free"] is True
    assert skipped == {
        "duplicate_copies": 1,
        "no_dates_in_window": 1,
        "not_approved": 1,
        "no_location": 1,
    }


class Client:
    def __init__(self, bodies):
        self.bodies = list(bodies)

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None):
        if "package_show" in url:
            return json.dumps(
                {"result": {"resources": [{"name": events.RESOURCE, "url": "https://x/f.json"}]}}
            ).encode()
        sink.write(self.bodies.pop(0))
        return b""


@pytest.mark.integration
def test_refresh_replaces_future_dates_and_serves_this_weeks_events(settings):
    first = json.dumps({"value": [record("a", days=(-0.5, 1, 2))]}).encode()
    cancelled = json.dumps({"value": [record("a", days=(-0.5, 2))]}).encode()
    client = Client([first, first, cancelled])
    url = settings.database_url
    assert events.refresh(url, client, NOW)["events"] == 1
    assert events.refresh(url, client, NOW)["status"] == "unchanged"
    # Tomorrow's date was cancelled; this morning's (already past) stays as history.
    assert events.refresh(url, client, NOW + timedelta(minutes=5))["status"] == "loaded"
    with connect(url) as conn:
        dates = [
            r["starts_at"]
            for r in conn.execute(
                "SELECT starts_at FROM normalized.city_event_occurrences ORDER BY starts_at"
            )
        ]
    assert [d - NOW for d in dates] == [timedelta(hours=-12), timedelta(days=2)]
    app = create_app(Settings(url), clock=lambda: NOW)
    body = TestClient(app).get("/api/events").json()
    [event] = body["events"]
    assert event["name"] == "Harvest Market" and len(event["dates"]) == 2
    assert "Open Government Licence" in body["attribution"]
