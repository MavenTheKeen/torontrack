"""City of Toronto road restrictions: parsing the feed's quirks, loading and removal."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from transit import roads
from transit.db import connect
from transit.http import FetchError
from transit.web.app import create_app

NOW = datetime(2026, 10, 13, 16, 0, tzinfo=UTC)


def ms(at: datetime) -> str:
    return str(int(at.timestamp() * 1000))


def item(rid: str, **overrides) -> dict:
    base = {
        "id": rid,
        "road": "Cole St",
        "name": f"Cole St restriction {rid}",
        "type": "ROAD_CLOSED",
        "latitude": "43.660160",
        "longitude": "-79.366080",
        "startTime": ms(NOW - timedelta(days=1)),
        "endTime": ms(NOW + timedelta(days=30)),
        "lastUpdated": ms(NOW - timedelta(days=2)),
        "geoPolyline": "[-79.366080,43.660160],[-79.365830,43.660210]",
        "maxImpact": "High",
        "currImpact": "High",
        "scheduleMonday": "07:00-18:00",
        "description": "Toronto-TMC: Water main",
        "source": '{"tabledata":[{"Option":"Transportation Services"',  # Truncated upstream.
    }
    return {**base, **overrides}


def body(items: list[dict]) -> bytes:
    return json.dumps({"Closure": items}).encode()


def test_a_stray_backslash_is_repaired_and_valid_escapes_are_kept():
    raw = b'{"Closure": [{"id": "1", "description": "Water \\ Sewer", "name": "a\\"b\\\\ c"}]}'
    [entry] = roads.decode(raw)["Closure"]
    assert entry["description"] == "Water \\ Sewer"
    assert entry["name"] == 'a"b\\ c'
    with pytest.raises(FetchError):
        roads.decode(b'{"Closure": [')


def test_parse_skips_unusable_entries_and_keeps_the_newest_duplicate():
    rows, skipped = roads.parse(
        roads.decode(
            body(
                [
                    item("1"),
                    item("2", type="CONSTRUCTION", geoPolyline=""),  # Falls back to its point.
                    item("3", startTime="soon"),
                    item("4", geoPolyline="[-80.5,43.4]", latitude="43.4", longitude="-80.5"),
                    item("5", type="UNKNOWN"),
                    item("1", name="Older copy", lastUpdated=ms(NOW - timedelta(days=9))),
                ]
            )
        )
    )
    assert skipped == 4
    by_id = {r["restriction_id"]: r for r in rows}
    assert set(by_id) == {"1", "2"}
    assert by_id["1"]["name"] == "Cole St restriction 1" and by_id["1"]["kind"] == "closure"
    assert by_id["1"]["path"] == [[-79.36608, 43.66016], [-79.36583, 43.66021]]
    assert by_id["2"]["path"] == [[-79.36608, 43.66016]] and by_id["2"]["kind"] == "construction"
    assert by_id["1"]["schedule"]["monday"] == "07:00-18:00"


class Client:
    def __init__(self, payloads):
        self.payloads = list(payloads)

    def fetch_bytes(self, url, *, host, max_bytes):
        assert host == roads.HOST
        return self.payloads.pop(0)


@pytest.mark.integration
def test_refresh_stores_changes_marks_removals_and_ignores_partial_feeds(settings, monkeypatch):
    monkeypatch.setattr(roads, "DROPOUT_MIN", 3)
    many = [item(str(i)) for i in range(10)]
    client = Client([body(many), body(many), body(many[:9]), body(many[:2]), body(many)])
    url = settings.database_url
    assert roads.refresh(url, client, NOW)["status"] == "loaded"
    assert roads.refresh(url, client, NOW + timedelta(minutes=10))["status"] == "unchanged"
    assert roads.refresh(url, client, NOW + timedelta(minutes=20))["restrictions"] == 9
    # Two of nine: a partial response, so nothing more is marked removed.
    assert roads.refresh(url, client, NOW + timedelta(minutes=30))["status"] == "dropout"
    with connect(url) as conn:
        removed = conn.execute(
            "SELECT restriction_id FROM normalized.road_restrictions WHERE removed_at IS NOT NULL"
        ).fetchall()
        assert [r["restriction_id"] for r in removed] == ["9"]
        assert (
            conn.execute("SELECT count(*) AS n FROM raw.road_restriction_responses").fetchone()["n"]
            == 3
        )
    # Back in the feed: no longer removed.
    roads.refresh(url, client, NOW + timedelta(minutes=40))
    with connect(url) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM normalized.road_restrictions WHERE removed_at IS NULL"
            ).fetchone()["n"]
            == 10
        )
        statuses = [
            r["status"]
            for r in conn.execute(
                "SELECT status FROM ops.road_restriction_refreshes ORDER BY refresh_id"
            )
        ]
    assert statuses == ["loaded", "unchanged", "loaded", "dropout", "loaded"]


@pytest.mark.integration
def test_endpoint_lists_only_restrictions_in_effect(settings):
    future = item("later", startTime=ms(NOW + timedelta(days=3)))
    roads.refresh(settings.database_url, Client([body([item("now"), future])]), NOW)
    client = TestClient(create_app(settings.database_url, clock=lambda: NOW))
    data = client.get("/api/roads").json()
    assert [r["restriction_id"] for r in data["restrictions"]] == ["now"]
    assert "Open Government Licence" in data["attribution"]
