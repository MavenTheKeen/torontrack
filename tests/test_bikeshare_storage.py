"""Bike Share storage keeps every value while storing repeated content once."""

import copy

import pytest

from tests.test_ingestion_integration import FixtureClient, scalar
from transit.bikeshare.ingestion import run
from transit.bikeshare.store import payload_hash
from transit.db import connect

pytestmark = pytest.mark.integration


class LaterClient(FixtureClient):
    """The same stations a minute later: new timestamps and counts, optionally a rename."""

    def __init__(self, rename=None):
        super().__init__()
        self.rename = rename
        self.returned = {}

    def fetch(self, url):
        payload = copy.deepcopy(super().fetch(url))
        payload["last_updated"] = "2026-10-09T09:40:47Z"
        stations = payload.get("data", {}).get("stations", [])
        if url.endswith("station_status"):
            for station in stations:
                station["num_vehicles_available"] += 1
                station["num_docks_available"] = max(station["num_docks_available"] - 1, 0)
        if url.endswith("station_information") and self.rename:
            for name in stations[0]["name"]:
                name["text"] = self.rename
        self.returned[url.rsplit("/", 1)[-1]] = payload
        return payload


def test_unchanged_content_is_stored_once_and_rebuilt_exactly(settings):
    run(settings, "first", client=FixtureClient())
    later = LaterClient()
    run(settings, "later", client=later)
    assert scalar(settings, "SELECT count(*) FROM raw.feed_payloads") == 6
    # Discovery and station information bodies are shared; status changed, so two.
    assert scalar(settings, "SELECT count(*) FROM raw.payload_bodies") == 4
    assert scalar(settings, "SELECT count(*) FROM normalized.station_versions") == 2
    assert scalar(settings, "SELECT count(*) FROM normalized.collections") == 2
    with connect(settings.database_url) as conn:
        rows = conn.execute(
            """SELECT feed_name, payload, payload_hash FROM raw.feed_payloads_full
               WHERE collection_id = 'later'"""
        ).fetchall()
    assert len(rows) == 3
    for row in rows:
        received = later.returned["gbfs.json" if row["feed_name"] == "gbfs" else row["feed_name"]]
        assert row["payload"] == received
        assert row["payload_hash"] == payload_hash(received)


def test_station_rename_adds_one_version_and_history_keeps_the_old_name(settings):
    run(settings, "first", client=FixtureClient())
    run(settings, "renamed", client=LaterClient(rename="Renamed Station"))
    assert scalar(settings, "SELECT count(*) FROM normalized.station_versions") == 3
    with connect(settings.database_url) as conn:
        names = {
            r["collection_id"]: r["name"]
            for r in conn.execute(
                """SELECT collection_id, name FROM normalized.station_observations
                   WHERE station_id = (SELECT min(station_id) FROM normalized.observations)
                   ORDER BY collection_id"""
            )
        }
    assert names["renamed"] == "Renamed Station"
    assert names["first"] != "Renamed Station"


def test_replay_reuses_the_collection_and_station_versions(settings):
    run(settings, "first", client=FixtureClient())
    key = scalar(settings, "SELECT collection_key FROM normalized.collections")
    run(settings, "first", replay=True)
    assert scalar(settings, "SELECT collection_key FROM normalized.collections") == key
    assert scalar(settings, "SELECT count(*) FROM normalized.station_versions") == 2
    assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 2
