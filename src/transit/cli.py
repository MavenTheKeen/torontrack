"""Torontrack: collect, replay and transform without an orchestrator."""

import argparse
import json
import sys
from datetime import UTC, date, datetime, time

from transit import (
    agencies,
    alerts,
    bike_lanes,
    events,
    exports,
    maptiles,
    planner,
    regional,
    roads,
    ticketmaster,
    weather,
)
from transit.bikeshare import trips as bikeshare_trips
from transit.bikeshare.ingestion import CollectionBusy, run
from transit.bikeshare.parsing import FeedValidationError, discover, parse_information, parse_status
from transit.config import DISCOVERY_URL, WEB_DB_ROLE, Settings
from transit.db import connect, ensure_readonly_role, init_db
from transit.http import FeedClient, FetchError
from transit.transform import run_transform
from transit.ttc import collector as ttc_collector
from transit.ttc import delays as ttc_delays
from transit.ttc import realtime as ttc_realtime
from transit.ttc import store as ttc_store
from transit.ttc.sources import REALTIME_FEEDS, REALTIME_HOST


def main():
    try:
        settings = Settings.from_env(require_database=False)
    except ValueError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init-db", help="Create schemas/tables (safe to repeat)")
    collect = commands.add_parser("collect", help="Collect live GBFS; reuse an ID only for a retry")
    collect.add_argument("--collection-id")
    replay = commands.add_parser("replay", help="Normalize stored raw feeds without HTTP")
    replay.add_argument("--collection-id", required=True)
    commands.add_parser("smoke", help="Explicit live API parse check; writes no database data")
    transform = commands.add_parser("transform", help="Run isolated dbt build and record results")
    transform.add_argument("--dbt-executable", default="dbt")
    transform.add_argument("--project-dir", default="dbt")
    transform.add_argument(
        "--full-refresh",
        action="store_true",
        help="Rebuild incremental models from retained data (after changing their columns)",
    )
    commands.add_parser("ttc-gtfs-refresh", help="Load TTC static GTFS if it changed")
    ttc_collect = commands.add_parser(
        "ttc-collect", help="Poll TTC realtime feeds (predictions and positions every 10 s)"
    )
    ttc_collect.add_argument("--once", action="store_true", help="Poll each feed once and exit")
    ttc_replay = commands.add_parser("ttc-replay", help="Re-normalize stored TTC snapshots")
    which = ttc_replay.add_mutually_exclusive_group(required=True)
    which.add_argument("--snapshot-id", type=int)
    which.add_argument("--pending", action="store_true", help="All not-yet-normalized")
    commands.add_parser("ttc-retention", help="Delete expired TTC raw snapshots and history")
    commands.add_parser("ttc-smoke", help="Explicit live TTC parse check; writes no data")
    commands.add_parser("ttc-health", help="Exit 1 unless every TTC feed polled in 2 minutes")
    delays = commands.add_parser(
        "ttc-delays-refresh", help="Load TTC's official subway delay log if it changed"
    )
    delays.add_argument("--reload", action="store_true", help="Re-normalize even if unchanged")
    weather_cmd = commands.add_parser(
        "weather-refresh", help="Load Environment Canada hourly weather for Toronto"
    )
    weather_cmd.add_argument(
        "--since", type=date.fromisoformat, help="YYYY-MM-DD; backfill from this day"
    )
    commands.add_parser("roads-refresh", help="Load City of Toronto road restrictions")
    commands.add_parser("events-refresh", help="Load the City of Toronto events calendar")
    commands.add_parser(
        "ticketed-events-refresh",
        help="Load concerts, games and shows from Ticketmaster (needs TICKETMASTER_API_KEY)",
    )
    trips_cmd = commands.add_parser(
        "bikeshare-trips-refresh", help="Load Bike Share ridership files that changed"
    )
    trips_cmd.add_argument(
        "--year", type=int, action="append", help="Reload this year regardless; repeatable"
    )
    commands.add_parser("regional-refresh", help="Load the regional train schedule if it changed")
    commands.add_parser(
        "agencies-refresh", help="Load Brampton Transit and MiWay schedules if they changed"
    )
    commands.add_parser(
        "agencies-collect", help="Poll Brampton Transit and MiWay bus positions every 15 s"
    )
    commands.add_parser(
        "agencies-health", help="Exit 1 unless every agency was polled in 2 minutes"
    )
    map_cmd = commands.add_parser(
        "map-tiles", help="Build the site's Toronto street map from OpenStreetMap"
    )
    map_cmd.add_argument("--out", default=settings.map_dir or "map-data")
    map_cmd.add_argument("--build", help="Protomaps build file, e.g. 20261013.pmtiles")
    lanes_cmd = commands.add_parser(
        "bike-lanes", help="Build the map's bike lane layer from the city's cycling network"
    )
    lanes_cmd.add_argument("--out", default=settings.map_dir or "map-data")
    osm_cmd = commands.add_parser(
        "osm-layers",
        help="Build the map's station entrance, parking and bike parking layers from OpenStreetMap",
    )
    osm_cmd.add_argument("--out", default=settings.map_dir or "map-data")
    osm_cmd.add_argument("--source", help="Local .osm.pbf to read instead of downloading one")
    planner_cmd = commands.add_parser(
        "planner-data", help="Download the trip planner's schedules and streets if they changed"
    )
    planner_cmd.add_argument("--out", default=settings.planner_dir or "planner-data")
    alerts_cmd = commands.add_parser("alerts", help="Send phone alerts on pipeline health")
    alerts_cmd.add_argument("--once", action="store_true", help="Check once and exit")
    export = commands.add_parser("export", help="Write completed days as open-data files")
    export.add_argument("--out", default=settings.export_dir or "exports")
    export.add_argument(
        "--day", type=date.fromisoformat, action="append", help="YYYY-MM-DD; repeatable"
    )
    export.add_argument("--force", action="store_true", help="Rewrite days already exported")
    args = parser.parse_args()
    try:
        if args.command == "ttc-smoke":
            client = FeedClient()
            try:
                result = {}
                for feed, url in REALTIME_FEEDS.items():
                    snapshot = ttc_realtime.decode(client.fetch_bytes(url, host=REALTIME_HOST))
                    result[feed] = {
                        "feed_timestamp": snapshot.feed_timestamp.isoformat(),
                        "entities": len(snapshot.message.entity),
                    }
            finally:
                client.close()
        elif args.command == "map-tiles":
            # Bike lanes and the OpenStreetMap layers are map data too; built after the
            # basemap, which they do not need. osm_layers needs pyosmium's native library,
            # which only the app image installs, so it is imported only here.
            from transit import osm_layers

            result = {
                "basemap": maptiles.build(args.out, build_key=args.build),
                "bike_lanes": bike_lanes.build(args.out),
                "osm_layers": osm_layers.build(args.out),
            }
        elif args.command == "bike-lanes":
            result = bike_lanes.build(args.out)
        elif args.command == "osm-layers":
            from transit import osm_layers

            result = osm_layers.build(args.out, source=args.source)
        elif args.command == "planner-data":
            client = FeedClient()
            try:
                result = planner.prepare(args.out, client)
            finally:
                client.close()
        elif args.command == "smoke":
            client = FeedClient()
            try:
                urls = discover(client.fetch(DISCOVERY_URL))
                stations = parse_information(client.fetch(urls["station_information"]))
                statuses = parse_status(client.fetch(urls["station_status"]))
                result = {
                    "live_api": "parsed",
                    "stations": len(stations),
                    "statuses": len(statuses),
                }
            finally:
                client.close()
        else:
            if settings.database_url is None:
                raise ValueError("Set DATABASE_URL before running database commands")
            if args.command.startswith("ttc-"):
                result = run_ttc(args, settings)
            elif args.command == "weather-refresh":
                client = FeedClient()
                try:
                    since = (
                        datetime.combine(args.since, time(), ttc_realtime.TORONTO)
                        if args.since
                        else None
                    )
                    result = weather.refresh(
                        settings.database_url, client, datetime.now(UTC), since
                    )
                finally:
                    client.close()
                if result["status"] == "failed":
                    raise FetchError(f"Weather refresh failed: {result['error']}")
            elif args.command == "bikeshare-trips-refresh":
                client = FeedClient()
                try:
                    result = bikeshare_trips.refresh(settings.database_url, client, args.year)
                finally:
                    client.close()
                if result["status"] == "failed":
                    raise FetchError(f"Ridership refresh failed: {result['error']}")
            elif args.command == "events-refresh":
                client = FeedClient()
                try:
                    result = events.refresh(settings.database_url, client)
                finally:
                    client.close()
                if result["status"] == "failed":
                    raise FetchError(f"Events refresh failed: {result['error']}")
            elif args.command == "ticketed-events-refresh":
                client = FeedClient()
                try:
                    result = ticketmaster.refresh(
                        settings.database_url, client, settings.ticketmaster_api_key
                    )
                finally:
                    client.close()
                if result["status"] == "failed":
                    raise FetchError(f"Ticketmaster refresh failed: {result['error']}")
            elif args.command == "roads-refresh":
                client = FeedClient()
                try:
                    result = roads.refresh(settings.database_url, client)
                finally:
                    client.close()
                if result["status"] == "failed":
                    raise FetchError(f"Road restrictions refresh failed: {result['error']}")
            elif args.command == "regional-refresh":
                client = FeedClient()
                try:
                    result = regional.refresh(settings.database_url, client)
                finally:
                    client.close()
                if result["status"] == "failed":
                    raise FetchError(f"Regional schedule refresh failed: {result['error']}")
            elif args.command == "agencies-refresh":
                client = FeedClient()
                try:
                    result = agencies.refresh(settings.database_url, client)
                finally:
                    client.close()
                failed = [k for k, r in result.items() if r["status"] == "failed"]
                if failed:
                    print(json.dumps(result, default=str))
                    raise FetchError(f"Agency schedule refresh failed for {', '.join(failed)}")
            elif args.command == "agencies-collect":
                agencies.run_forever(settings.database_url)
                result = {"collector": "stopped"}
            elif args.command == "agencies-health":
                with connect(settings.database_url) as conn:
                    late = agencies.healthy(conn, datetime.now(UTC))
                if late:
                    raise ValueError(f"No successful recent poll for {', '.join(late)}")
                result = {"agencies_collector": "healthy"}
            elif args.command == "alerts":
                alerts.run(
                    settings.database_url, settings.ntfy_url, settings.heartbeat_url, args.once
                )
                return 0
            elif args.command == "export":
                result = exports.export(
                    settings.database_url, args.out, datetime.now(UTC), args.day, args.force
                )
            elif args.command == "init-db":
                result = {"schema": "ready", "migrations_applied": init_db(settings.database_url)}
                # Deployments give the public site its own read-only login.
                if settings.web_db_password:
                    ensure_readonly_role(
                        settings.database_url, WEB_DB_ROLE, settings.web_db_password
                    )
                    result["readonly_role"] = WEB_DB_ROLE
            elif args.command == "transform":
                result = run_transform(
                    settings.database_url, args.dbt_executable, args.project_dir, args.full_refresh
                )
            else:
                result = run(settings, args.collection_id, replay=args.command == "replay")
        print(json.dumps(result, default=str))
    except (
        ValueError,
        FetchError,
        FeedValidationError,
        CollectionBusy,
        ttc_collector.CollectorBusy,
        agencies.CollectorBusy,
    ) as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Failed: {type(exc).__name__}. Check database/service health and ingestion attempts.",
            file=sys.stderr,
        )
        return 1
    return 0


def run_ttc(args, settings):
    database_url = settings.database_url
    raw_days = settings.ttc_raw_retention_days
    event_days = settings.ttc_event_retention_days
    if args.command == "ttc-gtfs-refresh":
        client = FeedClient()
        try:
            result = ttc_collector.refresh_static(database_url, client)
        finally:
            client.close()
        if result["status"] == "failed":
            raise FetchError(f"Static GTFS refresh failed: {result['error']}")
        return result
    if args.command == "ttc-delays-refresh":
        client = FeedClient()
        try:
            result = ttc_delays.refresh(database_url, client, reload=args.reload)
        finally:
            client.close()
        if result["status"] == "failed":
            raise FetchError(f"Delay log refresh failed: {result['error']}")
        return result
    if args.command == "ttc-collect":
        if not args.once:
            ttc_collector.run_forever(
                database_url,
                interval=settings.ttc_poll_seconds,
                fast_interval=settings.ttc_fast_poll_seconds,
                raw_days=raw_days,
                event_days=event_days,
                vehicle_raw_hours=settings.ttc_vehicle_raw_retention_hours,
            )
            return {"collector": "stopped"}
        client = FeedClient()
        try:
            return ttc_collector.poll_once(database_url, client)
        finally:
            client.close()
    with connect(database_url) as conn:
        if args.command == "ttc-health":
            healthy = conn.execute(
                """SELECT feed FROM ops.ttc_poll_runs
                   WHERE status <> 'failed' AND started_at > now() - interval '2 minutes'
                   GROUP BY feed"""
            ).fetchall()
            missing = set(REALTIME_FEEDS) - {r["feed"] for r in healthy}
            if missing:
                raise ValueError(f"No successful recent poll for {', '.join(sorted(missing))}")
            return {"ttc_collector": "healthy"}
        if args.command == "ttc-retention":
            return ttc_store.apply_retention(conn, raw_days, event_days)
        if args.snapshot_id is not None:
            return ttc_store.normalize_snapshot(conn, args.snapshot_id)
        pending = conn.execute(
            """SELECT snapshot_id FROM raw.ttc_realtime_snapshots
               WHERE normalized_at IS NULL ORDER BY feed_timestamp"""
        ).fetchall()
        return [ttc_store.normalize_snapshot(conn, r["snapshot_id"]) for r in pending]


if __name__ == "__main__":
    sys.exit(main())
