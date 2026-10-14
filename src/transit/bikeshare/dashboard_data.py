"""Read-only dashboard queries and explicit, testable display semantics."""

from datetime import UTC, datetime, timedelta
from math import ceil, floor
from zoneinfo import ZoneInfo

import pandas as pd
import psycopg
from psycopg.rows import dict_row

from transit.config import FUTURE_TOLERANCE_SECONDS, STALE_SECONDS

TORONTO = ZoneInfo("America/Toronto")
TIMESTAMP_COLUMNS = ("status_fetched_at", "source_published_at", "station_reported_at")


class AnalyticsNotBuilt(RuntimeError):
    """The database is reachable but the dbt staging view does not exist yet."""


def local_time(value) -> str:
    """Display an aware timestamp with its local zone, including across DST."""
    if value is None or pd.isna(value):
        return "Unknown"
    return pd.Timestamp(value).tz_convert(TORONTO).strftime("%Y-%m-%d %H:%M:%S %Z")


def freshness_reason(row, now: datetime) -> str:
    """A current reading requires all three clocks to be recent and credible."""
    for count in ("num_bikes_available", "num_docks_available"):
        if row.get(count) is None or pd.isna(row.get(count)):
            return "Missing status"
    for column in TIMESTAMP_COLUMNS:
        value = row.get(column)
        if value is None or pd.isna(value):
            return "Unknown timestamp"
        age = (pd.Timestamp(now) - pd.Timestamp(value)).total_seconds()
        if age < -FUTURE_TOLERANCE_SECONDS:
            return "Future timestamp"
        if age > STALE_SECONDS:
            return "Stale report"
    return "Fresh"


def annotate_current(stations: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Do not use a non-operational or stale count as an availability claim."""
    result = stations.copy()
    if result.empty:
        return result
    now = now or datetime.now(UTC)
    result["freshness"] = result.apply(lambda row: freshness_reason(row, now), axis=1)

    def state(row, service_flag, count, empty_label):
        if row["freshness"] != "Fresh":
            return row["freshness"]
        if pd.isna(row["is_installed"]) or not row["is_installed"]:
            return "Inactive"
        if pd.isna(row[service_flag]) or not row[service_flag]:
            return "Unavailable"
        return "Available" if row[count] > 0 else empty_label

    result["rental_state"] = result.apply(
        lambda row: state(row, "is_renting", "num_bikes_available", "Empty"), axis=1
    )
    result["return_state"] = result.apply(
        lambda row: state(row, "is_returning", "num_docks_available", "Full"), axis=1
    )
    return result


def load_overview(database_url: str) -> dict:
    """Read one consistent snapshot; startup and query failures remain visible."""
    with psycopg.connect(
        database_url,
        connect_timeout=5,
        options="-c statement_timeout=10000 -c timezone=UTC",
        row_factory=dict_row,
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        latest = conn.execute(
            """
            SELECT r.collection_id, r.collected_at, r.completed_at,
                   p.fetched_at, p.source_published_at
            FROM ops.ingestion_runs r
            LEFT JOIN LATERAL (
                SELECT fetched_at, source_published_at FROM raw.feed_payloads
                WHERE collection_id = r.collection_id AND feed_name = 'station_status'
                ORDER BY raw_id DESC LIMIT 1
            ) p ON true
            WHERE r.status = 'succeeded'
            ORDER BY r.collected_at DESC LIMIT 1
            """
        ).fetchone()
        failed = conn.execute(
            """
            SELECT count(*) AS count FROM ops.ingestion_attempts
            WHERE status = 'failed' AND started_at >= now() - interval '24 hours'
            """
        ).fetchone()["count"]
        runs = conn.execute(
            """
            SELECT collection_id, collected_at, completed_at, status
            FROM ops.ingestion_runs ORDER BY collected_at DESC LIMIT 10
            """
        ).fetchall()
        attempts = conn.execute(
            """SELECT collection_id, operation, started_at, finished_at, status, error
               FROM ops.ingestion_attempts ORDER BY attempt_id DESC LIMIT 10"""
        ).fetchall()
        transformation = conn.execute(
            """
            SELECT transformation_id, started_at, finished_at, status, dbt_results
            FROM ops.transformation_runs ORDER BY started_at DESC LIMIT 1
            """
        ).fetchone()
        stations, checks = [], []
        if latest:
            stations = conn.execute(
                """
                SELECT station_id, name, lat, lon, capacity, information_raw_id,
                       collected_at, status_fetched_at, source_published_at,
                       station_reported_at, num_bikes_available, num_docks_available,
                       num_bikes_disabled, num_docks_disabled,
                       is_installed, is_renting, is_returning, status_raw_id
                FROM normalized.station_observations
                WHERE collection_id = %s ORDER BY name, station_id
                """,
                (latest["collection_id"],),
            ).fetchall()
            checks = conn.execute(
                """
                SELECT check_name, passed, details FROM ops.quality_checks
                WHERE collection_id = %s ORDER BY check_name
                """,
                (latest["collection_id"],),
            ).fetchall()
        return {
            "latest": latest,
            "stations": pd.DataFrame(stations),
            "failed_attempts": failed,
            "runs": pd.DataFrame(runs),
            "attempts": pd.DataFrame(attempts),
            "checks": pd.DataFrame(checks),
            "transformation": transformation,
        }


def load_history(database_url: str, station_id: str, days: int, now=None) -> pd.DataFrame:
    """Use each collection's metadata; never fill gaps or derive trip counts."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    period_start = now - timedelta(days=days)
    with psycopg.connect(
        database_url,
        connect_timeout=5,
        options="-c statement_timeout=10000 -c timezone=UTC",
        row_factory=dict_row,
    ) as conn:
        rows = conn.execute(
            """
            SELECT s.collection_id, r.collected_at, s.status_fetched_at,
                   s.source_published_at, s.station_reported_at,
                   s.num_bikes_available, s.num_docks_available,
                   s.is_installed, s.is_renting, s.is_returning, s.status_raw_id
            FROM normalized.station_observations s
            JOIN ops.ingestion_runs r USING (collection_id)
            WHERE s.station_id = %s AND r.status = 'succeeded'
              AND r.collected_at >= %s AND r.collected_at < %s
            ORDER BY r.collected_at
            """,
            (station_id, period_start, now),
        ).fetchall()
    return pd.DataFrame(rows)


def prepare_history(history: pd.DataFrame) -> pd.DataFrame:
    """Evaluate old reports at collection time, preserving missing and stale gaps."""
    result = history.copy()
    if result.empty:
        return result
    result["freshness"] = result.apply(
        lambda row: freshness_reason(row, row["collected_at"]), axis=1
    )
    fresh_installed = (result["freshness"] == "Fresh") & result["is_installed"].eq(True)
    result["Bikes available"] = result["num_bikes_available"].where(
        fresh_installed & result["is_renting"].eq(True)
    )
    result["Docks available"] = result["num_docks_available"].where(
        fresh_installed & result["is_returning"].eq(True)
    )
    result["Toronto time"] = pd.to_datetime(result["collected_at"], utc=True).dt.tz_convert(TORONTO)
    return result


def complete_slot_window(now: datetime, days: int) -> tuple[datetime, datetime, int]:
    """Count only complete UTC 15-minute slots inside the rolling selected period."""
    if now.tzinfo is None or days not in (1, 7, 30):
        raise ValueError("Use an aware timestamp and a supported period (1, 7, or 30 days)")
    now = now.astimezone(UTC)
    start = datetime.fromtimestamp(ceil((now - timedelta(days=days)).timestamp() / 900) * 900, UTC)
    end = datetime.fromtimestamp(floor(now.timestamp() / 900) * 900, UTC)
    return start, end, int((end - start).total_seconds() / 900)


def load_analytics(database_url: str, station_id: str, days: int, now=None) -> dict:
    """Exact rolling-period SQL metrics from dbt's eligibility contract.

    The missing-view state is explicit. Query/connection errors propagate to a
    separate dashboard error, rather than being mistaken for an empty result.
    """
    now = now or datetime.now(UTC)
    slot_start, slot_end, expected_slots = complete_slot_window(now, days)
    now = now.astimezone(UTC)
    period_start = now - timedelta(days=days)
    with psycopg.connect(
        database_url,
        connect_timeout=5,
        options="-c statement_timeout=10000 -c timezone=UTC",
        row_factory=dict_row,
    ) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        exists = conn.execute(
            "SELECT to_regclass('staging.stg_station_observations') AS relation"
        ).fetchone()
        if exists["relation"] is None:
            raise AnalyticsNotBuilt("Run dbt build to create the analytics views")
        rankings = conn.execute(
            """
            SELECT station_id,
                   (array_agg(name ORDER BY collected_at DESC, collection_id DESC))[1] AS name,
                   count(*) AS metadata_snapshots,
                   count(*) FILTER (WHERE observed) AS observed_snapshots,
                   count(*) FILTER (WHERE rental_eligible) AS rental_eligible_snapshots,
                   count(*) FILTER (WHERE return_eligible) AS return_eligible_snapshots,
                   count(*) FILTER (
                       WHERE rental_eligible AND num_bikes_available = 0) AS empty_snapshots,
                   count(*) FILTER (
                       WHERE return_eligible AND num_docks_available = 0) AS full_snapshots,
                   100.0 * count(*) FILTER (WHERE rental_eligible AND num_bikes_available = 0)
                       / nullif(count(*) FILTER (WHERE rental_eligible), 0) AS empty_pct,
                   100.0 * count(*) FILTER (WHERE return_eligible AND num_docks_available = 0)
                       / nullif(count(*) FILTER (WHERE return_eligible), 0) AS full_pct
            FROM staging.stg_station_observations
            WHERE collected_at >= %s AND collected_at < %s
            GROUP BY station_id ORDER BY name, station_id
            """,
            (period_start, now),
        ).fetchall()
        hourly = conn.execute(
            """
            SELECT extract(hour FROM collected_at AT TIME ZONE 'America/Toronto')::int
                       AS toronto_hour,
                   count(*) AS metadata_snapshots,
                   count(*) FILTER (WHERE observed) AS observed_snapshots,
                   count(*) FILTER (WHERE rental_eligible) AS rental_eligible_snapshots,
                   count(*) FILTER (WHERE return_eligible) AS return_eligible_snapshots,
                   avg(num_bikes_available) FILTER (WHERE rental_eligible) AS mean_bikes,
                   avg(num_docks_available) FILTER (WHERE return_eligible) AS mean_docks
            FROM staging.stg_station_observations
            WHERE station_id = %s AND collected_at >= %s AND collected_at < %s
            GROUP BY toronto_hour ORDER BY toronto_hour
            """,
            (station_id, period_start, now),
        ).fetchall()
        observed_slots = conn.execute(
            """
            SELECT count(DISTINCT date_bin(
                interval '15 minutes', r.collected_at, timestamptz '2000-01-01 00:00:00+00'
            )) AS observed_slots
            FROM ops.ingestion_runs r
            WHERE r.status = 'succeeded' AND r.collected_at >= %s AND r.collected_at < %s
              AND EXISTS (SELECT 1 FROM normalized.collections c
                          JOIN normalized.observations o USING (collection_key)
                          WHERE c.collection_id = r.collection_id)
            """,
            (slot_start, slot_end),
        ).fetchone()["observed_slots"]
    return {
        "rankings": pd.DataFrame(rankings),
        "hourly": pd.DataFrame(hourly),
        "observed_slots": observed_slots,
        "expected_slots": expected_slots,
        "slot_start": slot_start,
        "slot_end": slot_end,
        "period_start": period_start,
        "period_end": now,
    }


def ranked_stations(rankings: pd.DataFrame, metric: str, minimum: int) -> pd.DataFrame:
    """Exclude an undefined denominator and show stable ties with sample support."""
    if metric not in ("empty", "full") or minimum < 1:
        raise ValueError("Choose empty/full and at least one eligible observation")
    if rankings.empty:
        return rankings.copy()
    denominator = "rental_eligible_snapshots" if metric == "empty" else "return_eligible_snapshots"
    result = rankings[rankings[denominator].ge(minimum) & rankings[f"{metric}_pct"].notna()].copy()
    result[f"{metric}_pct"] = pd.to_numeric(result[f"{metric}_pct"])
    return result.sort_values(
        [f"{metric}_pct", denominator, "name", "station_id"],
        ascending=[False, False, True, True],
    )
