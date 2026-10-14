"""Exercise real dbt SQL against controlled snapshots in a disposable PostgreSQL DB."""

import os
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_ingestion_integration import FixtureClient
from transit.bikeshare.ingestion import run
from transit.db import connect
from transit.transform import run_transform

pytestmark = pytest.mark.integration


class ScenarioClient(FixtureClient):
    def __init__(self, scenario):
        super().__init__()
        self.scenario = scenario

    def fetch(self, url):
        result = super().fetch(url)
        now = datetime.now(UTC)
        result["last_updated"] = now.isoformat()
        if url.endswith("station_status"):
            for row in result["data"]["stations"]:
                row["last_reported"] = now.isoformat()
            row = result["data"]["stations"][0]
            row.update(
                num_vehicles_available=0,
                num_docks_available=0,
                is_installed=True,
                is_renting=True,
                is_returning=True,
            )
            if self.scenario == "empty":
                row["num_docks_available"] = 4
            elif self.scenario == "full":
                row["num_vehicles_available"] = 4
            elif self.scenario == "inactive":
                row["is_installed"] = False
            elif self.scenario == "no_rentals":
                row["is_renting"] = False
            elif self.scenario == "no_returns":
                row["is_returning"] = False
            elif self.scenario == "stale":
                row["last_reported"] = (now - timedelta(hours=1)).isoformat()
            elif self.scenario == "future":
                row["last_reported"] = (now + timedelta(hours=1)).isoformat()
            elif self.scenario == "missing":
                result["data"]["stations"] = result["data"]["stations"][1:]
            elif self.scenario == "stale_source":
                result["last_updated"] = (now - timedelta(hours=1)).isoformat()
        return result


def test_dbt_build_and_metric_denominators(settings):
    executable = os.environ.get("DBT_EXECUTABLE")
    if not executable:
        pytest.skip("Set DBT_EXECUTABLE to the isolated dbt executable")
    scenarios = [
        "empty",
        "full",
        "inactive",
        "no_rentals",
        "no_returns",
        "stale",
        "future",
        "missing",
        "stale_source",
    ]
    for scenario in scenarios:
        run(settings, scenario, client=ScenarioClient(scenario))
    result = run_transform(settings.database_url, executable)
    assert result["status"] == "succeeded"
    with connect(settings.database_url) as conn:
        summary = conn.execute(
            "SELECT * FROM analytics.station_summary WHERE station_id='7000'"
        ).fetchone()
        assert summary["metadata_snapshots"] == 9
        assert summary["observed_snapshots"] == 8
        assert summary["rental_eligible_snapshots"] == 3
        assert summary["return_eligible_snapshots"] == 3
        assert summary["empty_snapshots"] == 2
        assert summary["full_snapshots"] == 2
        assert float(summary["empty_pct"]) == pytest.approx(200 / 3)
        assert float(summary["full_pct"]) == pytest.approx(200 / 3)
        assert float(summary["observation_coverage_pct"]) == pytest.approx(800 / 9)
        # Build uses views: changing eligibility is reflected without stale tables.
        conn.execute(
            "UPDATE normalized.observations SET is_installed=false WHERE station_id='7000'"
        )
        zero = conn.execute(
            "SELECT * FROM analytics.station_summary WHERE station_id='7000'"
        ).fetchone()
        assert zero["empty_pct"] is None
        assert zero["full_pct"] is None
        health = conn.execute("SELECT * FROM ops.transformation_runs").fetchone()
        assert health["status"] == "succeeded"
        assert all(r["status"] in ("pass", "success") for r in health["dbt_results"])


def test_failed_dbt_invocation_is_visible(settings):
    with pytest.raises(FileNotFoundError):
        run_transform(settings.database_url, "executable-that-does-not-exist-dbt")
    with connect(settings.database_url) as conn:
        row = conn.execute("SELECT * FROM ops.transformation_runs").fetchone()
        assert row["status"] == "failed"
        assert row["error"] == "FileNotFoundError"
