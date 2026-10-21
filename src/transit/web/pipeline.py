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
    "trips_surface": timedelta(minutes=3),
    "alerts_light_rail": timedelta(minutes=3),
    "bikeshare": timedelta(minutes=35),
    "dbt": timedelta(minutes=35),
    # Loaded hourly.
    "weather": timedelta(hours=2),
    # Loaded every 10 minutes.
    "roads": timedelta(minutes=30),
    # Live regional trains: positions and delays every 30 s, alerts every 2 minutes.
    "regional vehicles": timedelta(minutes=2),
    "regional trips": timedelta(minutes=2),
    "regional alerts": timedelta(minutes=10),
}
FEED_LABELS = {
    "trips_subway": "Subway train predictions",
    "alerts_subway": "Subway service alerts",
    "alerts_accessibility": "Elevator and escalator alerts",
    "vehicles": "Bus and streetcar positions",
    "trips_surface": "Bus and streetcar predictions",
    "alerts_light_rail": "Line 5 and 6 service alerts",
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


# Environment Canada publishes each local day once, 4 to 14 hours after it ends (observed
# October 2026), so the newest hour is normally up to about 38 hours old. Older than
# WEATHER_DATA_OK, the loader may be fine but the data is not.
WEATHER_DATA_OK = timedelta(hours=42)
WEATHER_DATA_FAILING = timedelta(hours=72)
STATUS_ORDER = ("ok", "delayed", "failing")


def worst(*statuses: str) -> str:
    return max(statuses, key=STATUS_ORDER.index)


def completeness(done: int, expected: int) -> str:
    """A job can have succeeded recently and still have missed many of its runs: 'ok' with
    at least 90% of the expected runs, 'delayed' with at least half, else 'failing'."""
    share = done / expected if expected else 1
    return "ok" if share >= 0.9 else "delayed" if share >= 0.5 else "failing"


def data_age_verdict(latest: datetime | None, ok: timedelta, failing: timedelta, now) -> str:
    """By the age of the newest data itself, not of the job that loads it."""
    if latest is None:
        return "failing"
    age = now - latest
    return "ok" if age <= ok else "delayed" if age <= failing else "failing"


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
                      count(*) FILTER (WHERE status = 'failed') AS failed
               FROM ops.ttc_poll_runs WHERE started_at > %s GROUP BY feed""",
            (now - timedelta(hours=1),),
        )
    }
    # Distinct records, not occurrences: an alert or train that keeps the same problem is
    # flagged again in every snapshot (every 10 or 30 s), which would count it ~120 times.
    # A snapshot-level issue (no entity, such as a feed dropout) counts once per snapshot.
    issues: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT i.feed, i.action, i.issue,
                  count(DISTINCT (coalesce(i.entity_id, '#' || i.snapshot_id),
                                  coalesce(i.stop_id, ''))) AS records
           FROM ops.ttc_record_issues i
           WHERE i.snapshot_id IN (SELECT snapshot_id FROM ops.ttc_poll_runs
                                   WHERE started_at > %s AND snapshot_id IS NOT NULL)
           GROUP BY 1, 2, 3 ORDER BY records DESC""",
        (now - timedelta(hours=1),),
    ):
        feed = issues.setdefault(r["feed"], {"rejected": 0, "flagged": 0, "flag_reasons": []})
        feed[r["action"]] = feed.get(r["action"], 0) + r["records"]
        if r["action"] == "flagged":
            feed["flag_reasons"].append({"issue": r["issue"], "records": r["records"]})
    feeds = []
    for feed, label in FEED_LABELS.items():
        success = last.get(feed, {}).get("finished_at")
        counts = {**(hour.get(feed) or {}), **issues.get(feed, {})}
        feeds.append(
            {
                "feed": feed,
                "label": label,
                "status": verdict(success, LIMITS[feed], now),
                "last_success": success,
                "age_seconds": _age(success, now),
                "last_hour": {
                    **{
                        k: int(counts.get(k, 0))
                        for k in ("polls", "stored", "unchanged", "failed", "rejected", "flagged")
                    },
                    "flag_reasons": counts.get("flag_reasons", []),
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
    expected = 96  # One collection every 15 minutes.
    return {
        "status": worst(
            verdict(success, LIMITS["bikeshare"], now),
            completeness(runs["succeeded_24h"], expected),
        ),
        "last_success": success,
        "age_seconds": _age(success, now),
        "succeeded_24h": runs["succeeded_24h"],
        "expected_24h": expected,
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
    latest = row["latest_observation"]
    return {
        "status": worst(
            verdict(row["last_success"], LIMITS["weather"], now),
            data_age_verdict(latest, WEATHER_DATA_OK, WEATHER_DATA_FAILING, now),
        ),
        "last_success": row["last_success"],
        "age_seconds": _age(row["last_success"], now),
        "latest_observation": latest,
        "data_age_seconds": _age(latest, now),
        "data_ok_seconds": round(WEATHER_DATA_OK.total_seconds()),
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


def regional(conn, now: datetime) -> dict | None:
    """Metrolinx's live feeds (transit.regional_live); None where no key is set and they
    have never been polled."""
    rows = conn.execute(
        "SELECT feed, succeeded_at, rows, error FROM ops.regional_polls ORDER BY feed"
    ).fetchall()
    if not rows:
        return None
    feeds = {
        r["feed"]: {
            "status": verdict(r["succeeded_at"], LIMITS[f"regional {r['feed']}"], now),
            "age_seconds": _age(r["succeeded_at"], now),
            "rows": r["rows"],
            "error": r["error"],
        }
        for r in rows
    }
    live = conn.execute(
        """SELECT count(*) AS trains FROM normalized.regional_live_trains
           WHERE NOT cancelled AND (reported_at > %(since)s OR delay_at > %(since)s)""",
        {"since": now - timedelta(minutes=3)},
    ).fetchone()
    return {
        "status": worst(*(f["status"] for f in feeds.values())),
        "feeds": feeds,
        "live_trains": live["trains"],
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
        """SELECT (SELECT min(first_seen_at) FROM normalized.ttc_train_stop_events) AS ttc_since,
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
    regional_part = regional(conn, now)
    if regional_part:
        parts["regional"] = regional_part
    statuses = [f["status"] for f in feeds] + [p["status"] for p in parts.values()]
    overall = "failing" if "failing" in statuses else "delayed" if "delayed" in statuses else "ok"
    return {
        "overall": overall,
        "ttc": {"feeds": feeds, "dropouts_today": ttc_dropouts_today(conn, now)},
        **parts,
        "official_delays": official_delays(conn),
        "data": data_volume(conn),
    }
