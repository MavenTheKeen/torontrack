"""The public site's database login can read what it needs and write nothing."""

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from transit.db import connect, ensure_readonly_role
from transit.web.app import create_app

pytestmark = pytest.mark.integration
ROLE = "transit_web_test"


@pytest.fixture
def readonly_url(settings):
    ensure_readonly_role(settings.database_url, ROLE, "first-password")
    ensure_readonly_role(settings.database_url, ROLE, "read-only-test")  # Safe to repeat.
    yield make_conninfo(settings.database_url, user=ROLE, password="read-only-test")
    with connect(settings.database_url) as conn:
        conn.execute("DROP TABLE IF EXISTS analytics.readonly_probe")
        conn.execute(f"DROP OWNED BY {ROLE}")
        conn.execute(f"DROP ROLE {ROLE}")


def test_readonly_role_reads_but_cannot_write(settings, readonly_url):
    with connect(settings.database_url) as owner:
        # A table dbt creates after the grant is readable too (default privileges).
        owner.execute("CREATE TABLE analytics.readonly_probe AS SELECT 1 AS x")
    with connect(readonly_url) as conn:
        assert conn.execute("SELECT x FROM analytics.readonly_probe").fetchone()["x"] == 1
        conn.execute("SELECT count(*) FROM normalized.ttc_current_predictions")
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("DELETE FROM ops.ttc_feed_state")
        conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM ops.ttc_feed_state")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM raw.ttc_realtime_snapshots")


def test_site_runs_on_the_readonly_role(readonly_url):
    from fastapi.testclient import TestClient

    client = TestClient(create_app(readonly_url))
    assert client.get("/health").status_code == 200
    assert client.get("/api/reliability").status_code == 200
