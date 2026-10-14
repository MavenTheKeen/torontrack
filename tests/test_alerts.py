"""Phone alerts: sustained problems alert once, recoveries alert, failed sends retry."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from transit import alerts
from transit.db import connect

pytestmark = pytest.mark.integration

T0 = datetime(2026, 10, 13, 14, 0, tzinfo=UTC)
NTFY = "https://ntfy.example/transit-test"


class Pipeline:
    """Stands in for the status-page checks, with each component's status settable."""

    def __init__(self, monkeypatch):
        self.status = dict.fromkeys(alerts.LABELS, "ok")
        monkeypatch.setattr(alerts, "components", self.components)

    def components(self, conn, now):
        return {
            name: {"status": status, "age_seconds": None if status == "failing" else 60}
            for name, status in self.status.items()
        }


@pytest.fixture
def pipeline(monkeypatch):
    return Pipeline(monkeypatch)


def phone(fail=False):
    sent = []

    def handle(request):
        if fail:
            return httpx.Response(503)
        sent.append((request.headers["Title"], request.headers["Priority"]))
        return httpx.Response(200)

    return httpx.Client(transport=httpx.MockTransport(handle)), sent


def at(settings, client, minutes):
    return alerts.check(settings.database_url, client, NTFY, T0 + timedelta(minutes=minutes))


def test_a_sustained_problem_alerts_once_then_recovers(settings, pipeline):
    client, sent = phone()
    at(settings, client, 0)
    pipeline.status["bikeshare"] = "delayed"
    at(settings, client, 1)
    at(settings, client, 3)  # Two minutes unhealthy: not yet.
    assert sent == []
    pipeline.status["bikeshare"] = "failing"  # Same incident; keeps its start time.
    at(settings, client, 4)
    at(settings, client, 5)
    assert sent == [("Bike Share collection: failing", "high")]
    pipeline.status["bikeshare"] = "ok"
    at(settings, client, 6)
    assert sent[-1] == ("Bike Share collection: recovered", "default")
    with connect(settings.database_url) as conn:
        assert conn.execute("SELECT count(*) AS n FROM ops.alerts").fetchone()["n"] == 2


def test_a_short_blip_never_alerts(settings, pipeline):
    client, sent = phone()
    pipeline.status["trips_subway"] = "delayed"
    at(settings, client, 0)
    at(settings, client, 2)
    pipeline.status["trips_subway"] = "ok"
    at(settings, client, 3)
    assert sent == []


def test_an_undelivered_alert_is_recorded_and_retried(settings, pipeline):
    down, _ = phone(fail=True)
    pipeline.status["dbt"] = "failing"
    at(settings, down, 0)
    at(settings, down, 3)
    client, sent = phone()
    at(settings, client, 4)
    assert sent == [("dbt build: failing", "high")]
    with connect(settings.database_url) as conn:
        rows = conn.execute("SELECT delivered, error FROM ops.alerts ORDER BY alert_id").fetchall()
    assert [(r["delivered"], r["error"]) for r in rows] == [
        (False, "HTTPStatusError"),
        (True, None),
    ]


def test_a_gap_in_monitoring_is_reported_when_it_resumes(settings, pipeline):
    client, sent = phone()
    at(settings, client, 0)
    at(settings, client, 1)
    assert sent == []
    at(settings, client, 12 * 60)  # The machine was off overnight.
    assert sent == [("Monitoring resumed", "default")]
