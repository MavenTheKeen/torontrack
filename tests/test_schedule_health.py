"""TTC schedule health: a refresh that fails, then a schedule that runs out."""

from datetime import UTC, datetime, time, timedelta

import pytest

from tests.ttc_helpers import T0, static_zip
from transit.db import connect
from transit.ttc import static_gtfs
from transit.web import pipeline

pytestmark = pytest.mark.integration


def test_an_expired_schedule_is_failing_and_a_failed_refresh_only_delayed(settings):
    with connect(settings.database_url) as conn:
        assert pipeline.schedule(conn, T0)["status"] == "failing"  # Nothing loaded.
        feed = static_gtfs.parse(static_zip())
        static_gtfs.load(conn, feed, "test://gtfs", T0)
        end = conn.execute(
            "SELECT max(service_end) AS d FROM normalized.ttc_gtfs_versions"
        ).fetchone()["d"]
        now = datetime.combine(end, time(12), UTC) - timedelta(days=5)
        conn.execute(
            """INSERT INTO ops.ttc_static_refreshes (started_at, finished_at, status)
               VALUES (%s, %s, 'unchanged')""",
            (now, now),
        )
        assert pipeline.schedule(conn, now)["status"] == "ok"

        later = now + timedelta(days=4)
        conn.execute(
            """INSERT INTO ops.ttc_static_refreshes (started_at, finished_at, status, error)
               VALUES (%s, %s, 'failed', 'empty')""",
            (later, later),
        )
        status = pipeline.schedule(conn, later)
        assert (status["status"], status["expired"], status["last_error"]) == (
            "delayed",
            False,
            "empty",
        )

        status = pipeline.schedule(conn, later + timedelta(days=2))
        assert (status["status"], status["expired"]) == ("failing", True)
