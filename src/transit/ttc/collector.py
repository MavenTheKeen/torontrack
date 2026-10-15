"""Always-on TTC collector: poll TTC's realtime feeds from one process.

Subway predictions and bus and streetcar positions are polled every `fast_interval`
seconds (10 by default): predictions change every second, and TTC publishes new vehicle
positions every 10-30 s, each already about 30 s old, so a slower poll would add up to its
own interval of delay on top. Alerts rarely change and are polled every `interval` seconds
(30). An unchanged feed is not stored again, so vehicle storage grows with TTC's own
updates rather than with the polling rate.
"""

import json
import signal
import threading
import time
from datetime import UTC, datetime

from transit import locks
from transit.bikeshare.parsing import FeedValidationError
from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc import passages, static_gtfs, store, surface
from transit.ttc.sources import (
    LIGHT_RAIL_LINES,
    REALTIME_FEEDS,
    REALTIME_HOST,
    STATIC_GTFS_URL,
    STATIC_HOST,
)

COLLECTOR_LOCK_ID = locks.TTC_COLLECTOR  # One TTC collector per database.
FAST_FEEDS = ("trips_subway", "vehicles")
RETENTION_EVERY_SECONDS = 3600
REINDEX_EVERY_SECONDS = 86400


class CollectorBusy(RuntimeError):
    pass


def _safe_error(exc: Exception) -> str:
    # Fetch/validation messages are written by this project; anything else may contain a DSN.
    if isinstance(exc, (FetchError, FeedValidationError)):
        return str(exc)[:1000]
    return type(exc).__name__


def refresh_static(database_url: str, client: FeedClient, url: str = STATIC_GTFS_URL) -> dict:
    started = datetime.now(UTC)
    with connect(database_url) as conn:
        try:
            payload = client.fetch_bytes(url, host=STATIC_HOST, max_bytes=200 * 1024 * 1024)
            feed = static_gtfs.parse(payload)
            loaded = static_gtfs.load(conn, feed, url, started)
            result = {
                "status": "loaded" if loaded else "unchanged",
                "feed_version": feed.feed_version,
            }
            # Light rail lines, also for a version loaded before they were included.
            if not loaded and (added := static_gtfs.add_lines(conn, feed)):
                result["lines_added"] = added
            # Buses and streetcars, also for a version loaded before they (or their
            # scheduled trip starts) were added, and without the light rail lines.
            if not surface.loaded(conn, feed.feed_version):
                network = surface.without_routes(surface.parse(payload), LIGHT_RAIL_LINES)
                result["surface"] = surface.load(conn, network, feed.feed_version)
            elif not surface.starts_loaded(conn, feed.feed_version):
                network = surface.without_routes(surface.parse(payload), LIGHT_RAIL_LINES)
                result["surface_starts"] = surface.load_starts(conn, network, feed.feed_version)
            if removed := surface.remove_routes(conn, feed.feed_version, LIGHT_RAIL_LINES):
                result["surface_removed"] = removed
        except Exception as exc:
            result = {"status": "failed", "feed_version": None, "error": _safe_error(exc)}
        conn.execute(
            """INSERT INTO ops.ttc_static_refreshes
               (started_at, finished_at, status, feed_version, error)
               VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], result["feed_version"], result.get("error")),
        )
    return result


def poll_feed(conn, client: FeedClient, feed: str, url: str) -> dict:
    started = datetime.now(UTC)
    result = {"feed": feed, "status": "failed", "snapshot_id": None, "feed_timestamp": None}
    try:
        backoff = conn.execute(
            "SELECT retry_not_before FROM ops.source_backoff WHERE discovery_url = %s", (url,)
        ).fetchone()
        if backoff and backoff["retry_not_before"] > started:
            raise FetchError("Source Retry-After is still active; no HTTP request made")
        payload = client.fetch_bytes(url, host=REALTIME_HOST, max_bytes=5 * 1024 * 1024)
        snapshot_id, is_new = store.store_snapshot(conn, feed, url, payload, started)
        result["snapshot_id"] = snapshot_id
        pending = conn.execute(
            """SELECT feed_timestamp, normalized_at IS NULL AS pending
               FROM raw.ttc_realtime_snapshots WHERE snapshot_id = %s""",
            (snapshot_id,),
        ).fetchone()
        result["feed_timestamp"] = pending["feed_timestamp"]
        if is_new or pending["pending"]:
            result.update(store.normalize_snapshot(conn, snapshot_id))
            result["status"] = "stored"
            if feed == "vehicles":
                record_passages(conn, started)
        else:
            result["status"] = "unchanged"
    except Exception as exc:
        result["error"] = _safe_error(exc)
        if isinstance(exc, FetchError) and exc.retry_not_before:
            conn.execute(
                """INSERT INTO ops.source_backoff (discovery_url, retry_not_before)
                   VALUES (%s, %s) ON CONFLICT (discovery_url) DO UPDATE
                   SET retry_not_before = greatest(ops.source_backoff.retry_not_before,
                                                  excluded.retry_not_before)""",
                (url, exc.retry_not_before),
            )
    conn.execute(
        """INSERT INTO ops.ttc_poll_runs
           (feed, started_at, finished_at, status, snapshot_id, feed_timestamp,
            accepted, rejected, flagged, error)
           VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s, %s)""",
        (
            feed,
            started,
            result["status"],
            result["snapshot_id"],
            result["feed_timestamp"],
            result.get("accepted"),
            result.get("rejected"),
            result.get("flagged"),
            result.get("error"),
        ),
    )
    return result


def record_passages(conn, now: datetime) -> None:
    """Stop passages are derived data: a failure is logged and the poll still counts."""
    try:
        version = store.active_feed_version(conn)
        if version:
            passages.record(conn, version, now)
    except Exception as exc:
        _log({"event": "passages_failed", "error": _safe_error(exc)})


def poll_once(database_url: str, client: FeedClient, feeds=None) -> list[dict]:
    """Poll `feeds` (default: all of them) once, in order."""
    with connect(database_url) as conn:
        return [
            poll_feed(conn, client, feed, url)
            for feed, url in REALTIME_FEEDS.items()
            if feeds is None or feed in feeds
        ]


def due_feeds(now: float, last: dict[str, float], interval: float, fast_interval: float):
    """Feeds whose interval has passed since their last poll (all of them at first)."""
    return [
        feed
        for feed in REALTIME_FEEDS
        if now - last.get(feed, float("-inf"))
        >= (fast_interval if feed in FAST_FEEDS else interval) - 0.5
    ]


def run_forever(
    database_url: str,
    *,
    interval: float = 30,
    fast_interval: float = 10,
    raw_days: int = 3,
    event_days: int = 90,
    vehicle_raw_hours: int = 6,
    client: FeedClient | None = None,
    stop: threading.Event | None = None,
):
    stop = stop or threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:  # Not the main thread (tests).
            pass
    client = client or FeedClient()
    # Held for the process lifetime: a second collector exits instead of double-polling.
    with connect(database_url) as lock_conn:
        if not lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (COLLECTOR_LOCK_ID,)
        ).fetchone()["locked"]:
            raise CollectorBusy("Another TTC collector is already running")
        with connect(database_url) as conn:
            has_static = store.active_feed_version(conn)
        if not has_static:
            _log({"event": "static_gtfs", **refresh_static(database_url, client)})
        last_retention = 0.0
        last_reindex = time.monotonic()
        summary = {"polls": 0, "stored": 0, "unchanged": 0, "failed": 0}
        last_polled: dict[str, float] = {}
        tick = min(interval, fast_interval)
        while not stop.is_set():
            started = time.monotonic()
            feeds = due_feeds(started, last_polled, interval, fast_interval)
            last_polled.update(dict.fromkeys(feeds, started))
            for result in poll_once(database_url, client, feeds):
                summary["polls"] += 1
                summary[result["status"]] += 1
                if result["status"] == "failed":
                    _log({"event": "poll_failed", **result})
            if started - last_retention >= RETENTION_EVERY_SECONDS:
                with connect(database_url) as conn:
                    deleted = store.apply_retention(
                        conn, raw_days, event_days, vehicle_raw_hours=vehicle_raw_hours
                    )
                _log({"event": "hourly", **summary, "retention_deleted": deleted})
                summary = dict.fromkeys(summary, 0)
                last_retention = started
            if started - last_reindex >= REINDEX_EVERY_SECONDS:
                # Housekeeping only: a failure is logged and retried tomorrow, never fatal.
                try:
                    with connect(database_url) as conn:
                        _log({"event": "reindex", **store.compact_event_indexes(conn)})
                except Exception as exc:
                    _log({"event": "reindex_failed", "error": _safe_error(exc)})
                last_reindex = started
            stop.wait(max(0.0, tick - (time.monotonic() - started)))
    client.close()


def _log(record: dict):
    print(json.dumps({"at": datetime.now(UTC).isoformat(), **record}, default=str), flush=True)
