from datetime import UTC, datetime, timedelta

import pytest

from tests.test_ingestion_integration import FixtureClient
from transit.bikeshare.dashboard_data import annotate_current, load_history, load_overview
from transit.bikeshare.ingestion import run
from transit.db import connect


@pytest.mark.integration
def test_dashboard_queries_against_postgres(settings):
    url = settings.database_url
    overview = load_overview(url)
    assert set(overview) >= {"latest", "stations", "checks", "runs", "failed_attempts"}
    assert len(annotate_current(overview["stations"])) == len(overview["stations"])
    assert load_history(url, "station-does-not-exist", 1).empty


@pytest.mark.integration
def test_history_uses_exact_utc_period_at_dst_boundary(settings):
    now = datetime(2026, 11, 1, 17, tzinfo=UTC)  # Toronto fall-back day.
    samples = {
        "too_early": now - timedelta(hours=24, minutes=30),
        "start_boundary": now - timedelta(hours=24),
        "inside": now - timedelta(hours=23),
        "end_boundary": now,
    }
    for identity, collected_at in samples.items():
        run(settings, identity, client=FixtureClient())
        with connect(settings.database_url) as conn:
            conn.execute(
                "UPDATE ops.ingestion_runs SET collected_at=%s WHERE collection_id=%s",
                (collected_at, identity),
            )
    history = load_history(settings.database_url, "7000", 1, now=now)
    assert list(history["collection_id"]) == ["start_boundary", "inside"]
