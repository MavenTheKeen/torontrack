"""Uses a disposable database ending in _test; never depends on the live API."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from transit.bikeshare.ingestion import CollectionBusy, run
from transit.bikeshare.store import LOCK_ID, payload_hash
from transit.db import connect
from transit.http import FetchError

pytestmark = pytest.mark.integration
FIXTURES = Path(__file__).parent / "fixtures"


class FixtureClient:
    def __init__(self):
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        name = "discovery" if url.endswith("gbfs.json") else url.rsplit("/", 1)[-1]
        return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def scalar(settings, sql):
    with connect(settings.database_url) as conn:
        return list(conn.execute(sql).fetchone().values())[0]


def test_duplicate_retry_replay_and_later_unchanged_observation(settings):
    client = FixtureClient()
    first = run(settings, "first", client=client)
    assert first["observations"] == 2
    assert len(client.calls) == 3
    assert run(settings, "first", client=client)["status"] == "already_succeeded"
    assert len(client.calls) == 3  # No unnecessary live refetch on retry.
    assert run(settings, "first", replay=True)["observations"] == 2
    assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 2
    run(settings, "later", client=client)
    assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 4
    assert scalar(settings, "SELECT count(DISTINCT collected_at) FROM normalized.collections") == 2
    assert scalar(settings, "SELECT count(*) FROM raw.feed_payloads") == 6
    with connect(settings.database_url) as conn:
        # Every payload rebuilt from envelope + deduplicated body is the one received.
        for row in conn.execute("SELECT * FROM raw.feed_payloads_full").fetchall():
            assert row["payload_hash"] == payload_hash(row["payload"])
            assert row["fetched_at"].utcoffset().total_seconds() == 0


def test_failed_normalized_write_rolls_back_and_replay_recovers(settings):
    with connect(settings.database_url) as conn:
        conn.execute("""CREATE OR REPLACE FUNCTION normalized.reject_test_write()
          RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN RAISE EXCEPTION 'injected failure'; END $$""")
        conn.execute("""CREATE TRIGGER reject_test_write BEFORE INSERT
          ON normalized.observations FOR EACH ROW
          EXECUTE FUNCTION normalized.reject_test_write()""")
    try:
        with pytest.raises(psycopg.errors.RaiseException):
            run(settings, "failed-write", client=FixtureClient())
        assert scalar(settings, "SELECT count(*) FROM normalized.collections") == 0
        assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 0
        assert scalar(settings, "SELECT count(*) FROM raw.feed_payloads") == 3
        assert scalar(settings, "SELECT status FROM ops.ingestion_runs") == "failed"
    finally:
        with connect(settings.database_url) as conn:
            conn.execute("DROP TRIGGER reject_test_write ON normalized.observations")
    run(settings, "failed-write", replay=True)
    assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 2
    assert (
        scalar(settings, "SELECT count(*) FROM ops.ingestion_attempts WHERE status='failed'") == 1
    )


def test_partial_fetch_failure_preserves_raw_and_retry_fetches_only_missing(settings):
    class FailingClient(FixtureClient):
        def fetch(self, url):
            if url.endswith("station_status"):
                raise FetchError("HTTP 503: retries exhausted")
            return super().fetch(url)

    with pytest.raises(FetchError):
        run(settings, "partial", client=FailingClient())
    assert scalar(settings, "SELECT count(*) FROM raw.feed_payloads") == 2
    assert scalar(settings, "SELECT status FROM ops.ingestion_runs") == "failed"
    client = FixtureClient()
    run(settings, "partial", client=client)
    assert len(client.calls) == 1
    assert client.calls[0].endswith("station_status")


def test_concurrent_collection_excluded_by_database_lock(settings):
    with connect(settings.database_url) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            with pytest.raises(CollectionBusy):
                run(settings, "overlap", client=FixtureClient())
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 0


def test_missing_status_is_quality_failure_without_fake_zero(settings):
    class MissingClient(FixtureClient):
        def fetch(self, url):
            result = super().fetch(url)
            if url.endswith("station_status"):
                result["data"]["stations"] = result["data"]["stations"][:1]
            return result

    run(settings, "missing", client=MissingClient())
    assert scalar(settings, "SELECT count(*) FROM normalized.station_observations") == 2
    assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 1
    assert scalar(settings, "SELECT count(*) FROM normalized.unobserved_stations") == 1
    assert (
        scalar(settings, "SELECT count(*) FROM normalized.station_observations WHERE observed") == 1
    )
    assert (
        scalar(
            settings,
            "SELECT passed FROM ops.quality_checks WHERE check_name='station_status_coverage'",
        )
        is False
    )


def test_failed_replay_preserves_previous_normalized_rows(settings, monkeypatch):
    run(settings, "good", client=FixtureClient())
    import transit.bikeshare.store as db

    original = db.parse_status

    def bad_count(payload):
        rows = original(payload)
        rows[0]["num_bikes_available"] = -1
        return rows

    monkeypatch.setattr(db, "parse_status", bad_count)
    with pytest.raises(psycopg.errors.CheckViolation):
        run(settings, "good", replay=True)
    assert scalar(settings, "SELECT count(*) FROM normalized.observations") == 2
    assert scalar(settings, "SELECT min(num_bikes_available) FROM normalized.observations") >= 0
    assert scalar(settings, "SELECT status FROM ops.ingestion_runs") == "succeeded"
    from transit.bikeshare.dashboard_data import load_overview

    assert load_overview(settings.database_url)["latest"]["collection_id"] == "good"


def test_retry_after_survives_new_process_and_new_collection_id(settings):
    retry_at = datetime.now(UTC) + timedelta(minutes=5)

    class RateLimited(FixtureClient):
        def fetch(self, url):
            raise FetchError("HTTP 429", retry_not_before=retry_at)

    with pytest.raises(FetchError):
        run(settings, "limited", client=RateLimited())
    client = FixtureClient()
    with pytest.raises(FetchError, match="still active"):
        run(settings, "next", client=client)
    assert client.calls == []
    assert (
        scalar(settings, "SELECT count(*) FROM ops.ingestion_attempts WHERE status='failed'") == 2
    )
    with connect(settings.database_url) as conn:
        conn.execute("UPDATE ops.source_backoff SET retry_not_before=now()-interval '1 second'")
    assert run(settings, "next", client=client)["status"] == "succeeded"
