"""Dashboard fixtures below are synthetic test rows, never served by the application."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from transit.bikeshare.dashboard_data import (
    AnalyticsNotBuilt,
    annotate_current,
    complete_slot_window,
    freshness_reason,
    local_time,
    prepare_history,
    ranked_stations,
)

APP = Path(__file__).resolve().parents[1] / "dashboard" / "app.py"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def station_row(now=NOW, **overrides):
    row = {
        "collection_id": "test-collection",
        "station_id": "fixture-7000",
        "name": "Synthetic test station",
        "lat": 43.64,
        "lon": -79.39,
        "capacity": 15,
        "information_raw_id": 1,
        "collected_at": now,
        "status_fetched_at": now,
        "source_published_at": now,
        "station_reported_at": now,
        "num_bikes_available": 4,
        "num_docks_available": 9,
        "num_bikes_disabled": 2,
        "num_docks_disabled": 0,
        "is_installed": True,
        "is_renting": True,
        "is_returning": True,
        "status_raw_id": 2,
    }
    return row | overrides


@pytest.mark.parametrize(
    "clock", ["status_fetched_at", "source_published_at", "station_reported_at"]
)
def test_each_clock_can_make_a_current_reading_stale(clock):
    assert (
        freshness_reason(station_row(**{clock: NOW - timedelta(minutes=31)}), NOW) == "Stale report"
    )


def test_exact_freshness_boundaries_and_future_tolerance():
    assert (
        freshness_reason(station_row(station_reported_at=NOW - timedelta(minutes=30)), NOW)
        == "Fresh"
    )
    assert (
        freshness_reason(station_row(station_reported_at=NOW + timedelta(minutes=5)), NOW)
        == "Fresh"
    )
    assert (
        freshness_reason(
            station_row(station_reported_at=NOW + timedelta(minutes=5, seconds=1)), NOW
        )
        == "Future timestamp"
    )


def test_missing_status_and_unknown_time_remain_explicit():
    assert freshness_reason(station_row(num_bikes_available=None), NOW) == "Missing status"
    assert freshness_reason(station_row(station_reported_at=None), NOW) == "Unknown timestamp"


def test_rental_and_return_eligibility_are_independent():
    rows = pd.DataFrame(
        [
            station_row(is_renting=False),
            station_row(is_returning=False),
            station_row(is_installed=False),
            station_row(num_bikes_available=0),
            station_row(num_docks_available=0),
        ]
    )
    result = annotate_current(rows, NOW)
    assert result["rental_state"].tolist() == [
        "Unavailable",
        "Available",
        "Inactive",
        "Empty",
        "Available",
    ]
    assert result["return_state"].tolist() == [
        "Available",
        "Unavailable",
        "Inactive",
        "Available",
        "Full",
    ]


def test_stale_counts_are_not_presented_as_available():
    result = annotate_current(pd.DataFrame([station_row(NOW - timedelta(hours=1))]), NOW)
    assert result.iloc[0]["rental_state"] == "Stale report"
    assert result.iloc[0]["return_state"] == "Stale report"


def test_history_uses_collection_time_and_preserves_exclusions():
    rows = pd.DataFrame(
        [
            station_row(),
            station_row(is_renting=False),
            station_row(station_reported_at=NOW - timedelta(hours=1)),
            station_row(num_bikes_available=None, num_docks_available=None),
        ]
    )
    history = prepare_history(rows)
    assert history.iloc[0]["Bikes available"] == 4
    assert history.iloc[1]["Docks available"] == 9
    assert pd.isna(history.iloc[1]["Bikes available"])
    assert history.iloc[2:][["Bikes available", "Docks available"]].isna().all().all()
    assert str(history["Toronto time"].dt.tz) == "America/Toronto"


def test_toronto_display_handles_dst_fallback_without_ambiguity():
    assert local_time(datetime(2026, 11, 1, 5, 30, tzinfo=UTC)).endswith("01:30:00 EDT")
    assert local_time(datetime(2026, 11, 1, 6, 30, tzinfo=UTC)).endswith("01:30:00 EST")
    assert local_time(None) == "Unknown"


def overview_fixture(now=None):
    now = now or datetime.now(UTC)
    return {
        "latest": {
            "collection_id": "test-collection",
            "collected_at": now,
            "completed_at": now,
            "fetched_at": now,
            "source_published_at": now,
        },
        "stations": pd.DataFrame([station_row(now)]),
        "failed_attempts": 0,
        "runs": pd.DataFrame(),
        "checks": pd.DataFrame(
            [{"check_name": "fixture_check", "passed": True, "details": {"rows": 1}}]
        ),
    }


def run_app(monkeypatch, overview, history=None, analytics=None):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test-only/not-connected")
    with (
        patch("transit.bikeshare.dashboard_data.load_overview", return_value=overview),
        patch(
            "transit.bikeshare.dashboard_data.load_history",
            return_value=(pd.DataFrame() if history is None else history),
        ),
        patch(
            "transit.bikeshare.dashboard_data.load_analytics",
            **(
                {"side_effect": AnalyticsNotBuilt()}
                if analytics is None
                else {"return_value": analytics}
            ),
        ),
    ):
        app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    return app


def test_dashboard_empty_state(monkeypatch):
    overview = overview_fixture()
    overview.update(latest=None, stations=pd.DataFrame(), checks=pd.DataFrame())
    app = run_app(monkeypatch, overview)
    assert any("No station observations yet" in info.value for info in app.info)
    assert len(app.selectbox) == 0


def test_dashboard_database_error_does_not_leak_credentials(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://private:secret@invalid/database")
    with patch(
        "transit.bikeshare.dashboard_data.load_overview", side_effect=RuntimeError("private:secret")
    ):
        app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert len(app.error) == 1
    assert "could not read the database" in app.error[0].value
    assert "secret" not in app.error[0].value


def test_dashboard_populated_state_and_history(monkeypatch):
    overview = overview_fixture()
    app = run_app(
        monkeypatch, overview, pd.DataFrame([station_row(overview["latest"]["collected_at"])])
    )
    assert len(app.error) == 0
    assert (
        next(metric.value for metric in app.metric if metric.label == "Stations with bikes") == "1"
    )
    assert any(box.label == "Select station" for box in app.selectbox)
    assert not any("No observed snapshots" in info.value for info in app.info)


def test_dashboard_stale_state(monkeypatch):
    app = run_app(monkeypatch, overview_fixture(datetime.now(UTC) - timedelta(hours=1)))
    assert any("stale" in warning.value for warning in app.warning)
    assert (
        next(metric.value for metric in app.metric if metric.label == "Stations with bikes") == "0"
    )


def test_dashboard_no_matching_stations(monkeypatch):
    overview = overview_fixture()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test-only/not-connected")
    with (
        patch("transit.bikeshare.dashboard_data.load_overview", return_value=overview),
        patch("transit.bikeshare.dashboard_data.load_history", return_value=pd.DataFrame()),
        patch("transit.bikeshare.dashboard_data.load_analytics", side_effect=AnalyticsNotBuilt()),
    ):
        app = AppTest.from_file(str(APP)).run()
        app.text_input[0].input("no-such-station").run()
    assert not app.exception
    assert any("No stations match" in info.value for info in app.info)


def test_coverage_window_excludes_partial_slots_and_counts_full_boundary():
    start, end, slots = complete_slot_window(NOW + timedelta(minutes=4), 1)
    assert start == NOW - timedelta(days=1) + timedelta(minutes=15)
    assert end == NOW
    assert slots == 95
    assert complete_slot_window(NOW, 1)[2] == 96


def test_coverage_window_uses_utc_across_dst():
    from zoneinfo import ZoneInfo

    local = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo("America/Toronto"))
    assert complete_slot_window(local, 1)[2] == 96


def analytics_fixture():
    return {
        "rankings": pd.DataFrame(
            [
                {
                    "station_id": "fixture-7000",
                    "name": "Synthetic test station",
                    "metadata_snapshots": 10,
                    "observed_snapshots": 9,
                    "rental_eligible_snapshots": 8,
                    "return_eligible_snapshots": 6,
                    "empty_snapshots": 4,
                    "full_snapshots": 1,
                    "empty_pct": 50.0,
                    "full_pct": 100 / 6,
                },
                {
                    "station_id": "fixture-missing",
                    "name": "Missing status fixture",
                    "metadata_snapshots": 10,
                    "observed_snapshots": 0,
                    "rental_eligible_snapshots": 0,
                    "return_eligible_snapshots": 0,
                    "empty_snapshots": 0,
                    "full_snapshots": 0,
                    "empty_pct": None,
                    "full_pct": None,
                },
            ]
        ),
        "hourly": pd.DataFrame(
            [
                {
                    "toronto_hour": 8,
                    "metadata_snapshots": 10,
                    "observed_snapshots": 9,
                    "rental_eligible_snapshots": 8,
                    "return_eligible_snapshots": 6,
                    "mean_bikes": 2.0,
                    "mean_docks": 5.0,
                },
            ]
        ),
        "observed_slots": 3,
        "expected_slots": 96,
        "slot_start": NOW - timedelta(days=1),
        "slot_end": NOW,
        "period_start": NOW - timedelta(days=1),
        "period_end": NOW,
    }


def test_rankings_filter_on_each_metric_denominator_and_exclude_undefined():
    frame = analytics_fixture()["rankings"]
    assert len(ranked_stations(frame, "empty", 7)) == 1
    assert ranked_stations(frame, "full", 7).empty
    assert len(ranked_stations(frame, "full", 1)) == 1


def test_dashboard_analytics_missing_is_a_specific_build_state(monkeypatch):
    app = run_app(monkeypatch, overview_fixture())
    assert any("Analytics are not built yet" in info.value for info in app.info)
    assert not app.error


def test_dashboard_analytics_query_failure_is_not_empty_state(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test-only/not-connected")
    with (
        patch("transit.bikeshare.dashboard_data.load_overview", return_value=overview_fixture()),
        patch("transit.bikeshare.dashboard_data.load_history", return_value=pd.DataFrame()),
        patch(
            "transit.bikeshare.dashboard_data.load_analytics",
            side_effect=RuntimeError("private:secret"),
        ),
    ):
        app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    assert any("Analytics could not be read" in error.value for error in app.error)
    assert not any("secret" in error.value for error in app.error)


def test_dashboard_analytics_rankings_and_coverage(monkeypatch):
    app = run_app(monkeypatch, overview_fixture(), analytics=analytics_fixture())
    assert not app.error
    assert (
        next(metric.value for metric in app.metric if metric.label == "System collection coverage")
        == "3.1%"
    )
    assert [tab.label for tab in app.tabs] == ["Frequently empty", "Frequently full"]
    assert any("1 of 2 stations" in caption.value for caption in app.caption)


def test_dashboard_analytics_empty_period(monkeypatch):
    analytics = analytics_fixture()
    analytics.update(rankings=pd.DataFrame(), hourly=pd.DataFrame(), observed_slots=0)
    app = run_app(monkeypatch, overview_fixture(), analytics=analytics)
    assert not app.error
    assert any("No station snapshots in the selected period" in info.value for info in app.info)


def test_dashboard_failed_transformation_is_visible(monkeypatch):
    overview = overview_fixture()
    overview["transformation"] = {
        "transformation_id": "test-build",
        "started_at": NOW,
        "finished_at": NOW,
        "status": "failed",
        "dbt_results": [
            {
                "unique_id": "test.synthetic.example",
                "status": "fail",
                "failures": 1,
                "execution_time": 0.01,
            },
        ],
    }
    app = run_app(monkeypatch, overview)
    assert any("latest dbt build failed" in warning.value for warning in app.warning)
