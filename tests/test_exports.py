"""Open-data export: complete days only, files that match their sidecars, safe reruns."""

import gzip
import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tests.test_ingestion_integration import FixtureClient
from transit import exports
from transit.bikeshare.ingestion import run
from transit.ttc.realtime import TORONTO
from transit.web.app import create_app


def test_a_day_is_complete_after_the_service_day_ends_at_4am():
    day = date(2026, 10, 9)
    assert exports.last_complete_day(
        datetime(2026, 10, 10, 3, 59, tzinfo=TORONTO)
    ) == day - timedelta(1)
    assert exports.last_complete_day(datetime(2026, 10, 10, 4, 0, tzinfo=TORONTO)) == day


def test_day_bounds_follow_daylight_saving():
    start, end = exports.service_day_bounds(date(2026, 11, 1))  # Clocks go back: 25 hours.
    assert end - start == timedelta(hours=25)
    assert start == datetime(2026, 11, 1, 4, tzinfo=UTC)


@pytest.mark.integration
def test_export_writes_verifiable_files_once_and_the_site_serves_them(settings, tmp_path):
    run(settings, "first", client=FixtureClient())
    collected = datetime.now(UTC).astimezone(TORONTO).date()
    later = datetime.combine(collected + timedelta(2), datetime.min.time(), TORONTO)
    assert exports.export(settings.database_url, tmp_path, datetime.now(UTC)) == {}
    written = exports.export(settings.database_url, tmp_path, later)
    assert written["bikeshare-availability"] == [str(collected)]

    folder = tmp_path / "bikeshare-availability"
    sidecar = json.loads((folder / f"{collected}.json").read_text(encoding="utf-8"))
    assert sidecar["rows"] == 2 and sidecar["partial"] is True
    parquet = folder / f"{collected}.parquet"
    assert pq.read_table(parquet).num_rows == 2
    assert sidecar["files"]["parquet"]["sha256"] == hashlib.sha256(parquet.read_bytes()).hexdigest()
    csv = gzip.decompress((folder / f"{collected}.csv.gz").read_bytes()).decode().splitlines()
    assert csv[0].startswith('"collection_id","collected_at","station_id"') and len(csv) == 3
    assert not list(tmp_path.rglob("*.partial"))

    # A rerun leaves finished days alone.
    assert "bikeshare-availability" not in exports.export(settings.database_url, tmp_path, later)

    client = TestClient(create_app(settings.database_url, export_dir=str(tmp_path)))
    listing = client.get("/api/datasets").json()
    assert listing["available"] is True
    bikes = next(d for d in listing["datasets"] if d["name"] == "bikeshare-availability")
    path = bikes["files"][0]["files"]["parquet"]["path"]
    download = client.get(f"/data/{path}")
    assert download.status_code == 200 and download.content == parquet.read_bytes()
    assert client.get("/data/../pyproject.toml").status_code == 404


@pytest.mark.integration
def test_datasets_endpoint_without_exports(settings, tmp_path):
    client = TestClient(create_app(settings.database_url, export_dir=str(tmp_path / "none")))
    assert client.get("/api/datasets").json() == {"available": False}
