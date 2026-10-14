"""Pipeline health for the public status page, read from the ops tables each collector
and the dbt runner already write. Nothing here is estimated: every number is a count or
timestamp recorded when the work happened."""

from datetime import datetime, timedelta

from transit.ttc.realtime import TORONTO

# How long each part may go without a success before it counts as delayed. TTC feeds
# are polled every 30 s; Bike Share collection and dbt run every 15 minutes.
LIMITS = {
    "trips_subway": timedelta(minutes=2),
    "alerts_subway": timedelta(minutes=3),
    "alerts_accessibility": timedelta(minutes=3),
    "vehicles": timedelta(minutes=2),
    "bikeshare": timedelta(minutes=35),
    "dbt": timedelta(minutes=35),
    # Loaded hourly; Environment Canada itself publishes hours several hours late.
    "weather": timedelta(hours=2),
    # Loaded every 10 minutes.
    "roads": timedelta(minutes=30),
}
FEED_LABELS = {
    "trips_subway": "Subway train predictions",
    "alerts_subway": "Subway service alerts",
    "alerts_accessibility": "Elevator and escalator alerts",
    "vehicles": "Bus and streetcar positions",
}
# Row counts come from planner statistics (pg_class.reltuples): close, and free to read.
VOLUME_TABLES = {
    "ttc_train_visits": "normalized.ttc_train_stop_events",
    "ttc_headways": "analytics.ttc_headways",
    "bikeshare_observations": "normalized.observations",
    "bikeshare_station_versions": "normalized.station_versions",
}


def verdict(last_success: datetime | None, limit: timedelta, now: datetime) -> str:
    """By time since the last success only, so one failed poll that the collector retries
    does not flicker the page: 'ok' within the limit, 'delayed' up to four times it,
    'failing' beyond that or if it never succeeded."""
    if last_success is None:
        return "failing"
    age = now - last_success
    return "ok" if age <= limit else "delayed" if age <= 4 * limit else "failing"


def _age(at: datetime | None, now: datetime) -> int | None:
    return None if at is None else max(0, round((now - at).total_seconds()))


def ttc_feeds(conn, now: datetime) -> list[dict]:
    last = {
        r["feed"]: r
        for r in conn.execute(
            """SELECT DISTINCT ON (feed) feed, finished_at, status
               FROM ops.ttc_poll_runs WHERE status <> 'failed'
               ORDER BY feed, started_at DESC"""
        )
    }
    hour = {
        r["feed"]: r
        for r in conn.execute(
            """SELECT feed, count(*) AS polls,
                      count(*) FILTER (WHERE status = 'stored') AS stored,
                      count(*) FILTER (WHERE status = 'unchanged') AS unchanged,
                      count(*) FILTER (WHERE status = 'failed') AS failed,
                      coalesce(sum(rejected), 0) AS rejected,
                      coalesce(sum(flagged), 0) AS flagged
               FROM ops.ttc_poll_runs WHERE started_at > %s GROUP BY feed""",
            (now - timedelta(hours=1),),
        )
    }
    feeds = []
    for feed, label in FEED_LABELS.items():
        success = last.get(feed, {}).get("finished_at")
        counts = hour.get(feed) or {}
        feeds.append(
            {
                "feed": feed,
                "label": label,
                "status": verdict(success, LIMITS[feed], now),
                "last_success": success,
                "age_seconds": _age(success, now),
                "last_hour": {
                    k: int(counts.get(k, 0))
                    for k in ("polls", "stored", "unchanged", "failed", "rejected", "flagged")
                },
            }
        )
    return feeds


def ttc_dropouts_today(conn, now: datetime) -> dict:
    """Trip snapshots skipped today because most trains vanished (see docs/ttc.md)."""
    start = now.astimezone(TORONTO).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        """SELECT count(*) AS snapshots,
                  count(*) FILTER (WHERE EXISTS (
                      SELECT 1 FROM ops.ttc_record_issues i
                      WHERE i.snapshot_id = r.snapshot_id AND i.issue = 'feed_dropout'))
                    AS dropouts
           FROM ops.ttc_poll_runs r
           WHERE feed = 'trips_subway' AND status = 'stored' AND started_at >= %s""",
        (start,),
    ).fetchone()
    snapshots, dropouts = row["snapshots"], row["dropouts"]
    return {
        "snapshots": snapshots,
        "dropouts": dropouts,
        "pct": round(100 * dropouts / snapshots, 1) if snapshots else None,
    }


def bikeshare(conn, now: datetime) -> dict:
    runs = conn.execute(
        """SELECT max(collected_at) FILTER (WHERE status = 'succeeded') AS last_success,
                  count(*) FILTER (WHERE status = 'succeeded' AND collected_at > %(day)s)
                    AS succeeded_24h,
                  (SELECT count(*) FROM ops.ingestion_attempts
                   WHERE status = 'failed' AND started_at > %(day)s) AS failed_attempts_24h
           FROM ops.ingestion_runs""",
        {"day": now - timedelta(hours=24)},
    ).fetchone()
    checks = conn.execute(
        """SELECT check_name, passed, details FROM ops.quality_checks
           WHERE collection_id = (SELECT collection_id FROM ops.ingestion_runs
                                  WHERE status = 'succeeded'
                                  ORDER BY collected_at DESC LIMIT 1)
           ORDER BY check_name"""
    ).fetchall()
    success = runs["last_success"]
    return {
        "status": verdict(success, LIMITS["bikeshare"], now),
        "last_success": success,
        "age_seconds": _age(success, now),
        "succeeded_24h": runs["succeeded_24h"],
        "expected_24h": 96,
        "failed_attempts_24h": runs["failed_attempts_24h"],
        "quality_checks": [dict(c) for c in checks],
    }


def dbt(conn, now: datetime) -> dict:
    latest = conn.execute(
        """SELECT status, started_at, finished_at,
                  (SELECT count(*) FROM jsonb_array_elements(dbt_results) e
                   WHERE e->>'status' IN ('pass', 'success')) AS passed,
                  (SELECT count(*) FROM jsonb_array_elements(dbt_results) e
                   WHERE e->>'status' = 'warn') AS warned,
                  (SELECT count(*) FROM jsonb_array_elements(dbt_results) e
                   WHERE e->>'status' IN ('fail', 'error')) AS failed
           FROM ops.transformation_runs ORDER BY started_at DESC LIMIT 1"""
    ).fetchone()
    success = conn.execute(
        """SELECT max(finished_at) AS at FROM ops.transformation_runs
           WHERE status = 'succeeded'"""
    ).fetchone()["at"]
    return {
        "status": verdict(success, LIMITS["dbt"], now),
        "last_success": success,
        "age_seconds": _age(success, now),
        "latest": dict(latest) if latest else None,
    }


def weather(conn, now: datetime) -> dict:
    row = conn.execute(
        """SELECT (SELECT max(finished_at) FROM ops.weather_refreshes
                   WHERE status = 'loaded') AS last_success,
                  max(observed_at) AS latest_observation, count(*) AS hours
           FROM normalized.weather_hourly"""
    ).fetchone()
    return {
        "status": verdict(row["last_success"], LIMITS["weather"], now),
        "last_success": row["last_success"],
        "age_seconds": _age(row["last_success"], now),
        "latest_observation": row["latest_observation"],
        "hours": row["hours"],
    }


def roads(conn, now: datetime) -> dict:
    row = conn.execute(
        """SELECT (SELECT max(finished_at) FROM ops.road_restriction_refreshes
                   WHERE status IN ('loaded', 'unchanged')) AS last_success,
                  count(*) FILTER (WHERE starts_at <= %(now)s AND ends_at > %(now)s)
                      AS in_effect
           FROM normalized.road_restrictions WHERE removed_at IS NULL""",
        {"now": now},
    ).fetchone()
    return {
        "status": verdict(row["last_success"], LIMITS["roads"], now),
        "last_success": row["last_success"],
        "age_seconds": _age(row["last_success"], now),
        "in_effect": row["in_effect"],
    }


def official_delays(conn) -> dict | None:
    """The latest check of TTC's own delay log, and how far that log reaches."""
    if conn.execute("SELECT to_regclass('ops.ttc_delay_refreshes') AS r").fetchone()["r"] is None:
        return None
    row = conn.execute(
        """SELECT finished_at, status,
                  (SELECT max(latest_delay_at) FROM ops.ttc_delay_refreshes
                   WHERE status = 'loaded') AS log_reaches,
                  (SELECT max(finished_at) FROM ops.ttc_delay_refreshes
                   WHERE status = 'loaded') AS last_loaded
           FROM ops.ttc_delay_refreshes ORDER BY refresh_id DESC LIMIT 1"""
    ).fetchone()
    return dict(row) if row else None


def data_volume(conn) -> dict:
    counts = {
        name: r["rows"]
        for name, table in VOLUME_TABLES.items()
        for r in conn.execute(
            """SELECT CASE WHEN to_regclass(%(t)s) IS NULL THEN NULL
                           ELSE greatest((SELECT reltuples FROM pg_class
                                          WHERE oid = to_regclass(%(t)s)), 0)::bigint
                      END AS rows""",
            {"t": table},
        )
    }
    since = conn.execute(
        """SELECT (SELECT min(fetched_at) FROM raw.ttc_realtime_snapshots) AS ttc_raw_since,
                  (SELECT min(first_seen_at) FROM normalized.ttc_train_stop_events) AS ttc_since,
                  (SELECT min(collected_at) FROM normalized.collections) AS bikeshare_since,
                  pg_database_size(current_database()) AS database_bytes,
                  (SELECT max(version) FROM ops.schema_migrations) AS schema_version"""
    ).fetchone()
    return {"rows": counts, **dict(since)}


def pipeline_status(conn, now: datetime) -> dict:
    feeds = ttc_feeds(conn, now)
    parts = {
        "bikeshare": bikeshare(conn, now),
        "dbt": dbt(conn, now),
        "weather": weather(conn, now),
        "roads": roads(conn, now),
    }
    statuses = [f["status"] for f in feeds] + [p["status"] for p in parts.values()]
    overall = "failing" if "failing" in statuses else "delayed" if "delayed" in statuses else "ok"
    return {
        "overall": overall,
        "ttc": {"feeds": feeds, "dropouts_today": ttc_dropouts_today(conn, now)},
        **parts,
        "official_delays": official_delays(conn),
        "data": data_volume(conn),
    }
