"""Regional rail from Metrolinx's static GTFS: lines, stations, track shapes and
every scheduled trip, loaded as an immutable feed version like the TTC schedule.

Metrolinx's licence excludes its trademarks from use with the data, so the site calls these
regional trains and drops the brand suffix from station names and destinations. Live train
positions need a registered Metrolinx API key; until then the site places trains by
timetable and says so.
"""

import csv
import hashlib
import io
import re
import zipfile
from collections import defaultdict
from datetime import UTC, date, datetime

from psycopg.types.json import Jsonb

from transit.bikeshare.parsing import FeedValidationError
from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.realtime import TORONTO
from transit.ttc.static_gtfs import gtfs_date, seconds

HOST = "assets.metrolinx.com"
GTFS_URL = f"https://{HOST}/raw/upload/Documents/Metrolinx/Open%20Data/GO-GTFS.zip"
RAIL_ROUTE_TYPE = "2"
ATTRIBUTION = "Data used in this product or service is provided with the permission of Metrolinx."
REQUIRED_FILES = ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")


def clean_name(name: str) -> str:
    """'Union Station GO' -> 'Union Station'; 'Hamilton GO Centre' -> 'Hamilton Centre'."""
    name = re.sub(r"\s+GO\s+Centre$", " Centre", name.strip())
    return re.sub(r"\s+GO(\s+Station)?$", "", name)


def destination(headsign: str) -> str:
    """'MI - Union Station GO' -> 'Union Station'."""
    return clean_name(re.sub(r"^[A-Z]{2}\s+-\s+", "", headsign.strip()))


def _rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))


def parse(zip_bytes: bytes) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise FeedValidationError("Regional GTFS is not a valid zip archive") from exc
    names = archive.namelist()
    missing = [n for n in REQUIRED_FILES if n not in names]
    if missing:
        raise FeedValidationError(f"Regional GTFS is missing {', '.join(missing)}")

    lines = [
        {
            "line_id": r["route_id"],
            "code": r["route_short_name"],
            "name": r["route_long_name"],
            "colour": r.get("route_color") or None,
            "text_colour": r.get("route_text_color") or None,
        }
        for r in _rows(archive, "routes.txt")
        if r["route_type"] == RAIL_ROUTE_TYPE
    ]
    line_ids = {line["line_id"] for line in lines}
    if not line_ids:
        raise FeedValidationError("Regional GTFS contains no rail routes")

    trips = {
        r["trip_id"]: {
            "trip_id": r["trip_id"],
            "line_id": r["route_id"],
            "service_id": r["service_id"],
            "direction_id": int(r["direction_id"]) if r.get("direction_id") else None,
            "destination": destination(r.get("trip_headsign") or ""),
            "shape_id": r.get("shape_id") or None,
        }
        for r in _rows(archive, "trips.txt")
        if r["route_id"] in line_ids
    }

    # Rows come in no particular order (descending within a trip in practice).
    by_trip = defaultdict(list)
    for r in _rows(archive, "stop_times.txt"):
        if r["trip_id"] not in trips:
            continue
        arrival = r["arrival_time"] or r["departure_time"]
        departure = r["departure_time"] or r["arrival_time"]
        if not arrival:
            continue  # An untimed stop cannot place a train; timed neighbours still do.
        by_trip[r["trip_id"]].append(
            (int(r["stop_sequence"]), r["stop_id"], seconds(arrival), seconds(departure))
        )
    stop_times = []
    for trip_id, rows in by_trip.items():
        rows.sort()
        if len(rows) < 2:
            continue
        stop_times.extend((trip_id, *row) for row in rows)
    timed = set(by_trip)
    trips = {k: v for k, v in trips.items() if k in timed and len(by_trip[k]) >= 2}
    station_ids = {row[2] for row in stop_times}

    stations = []
    for r in _rows(archive, "stops.txt"):
        if r["stop_id"] in station_ids:
            stations.append(
                {
                    "station_id": r["stop_id"],
                    "name": clean_name(r["stop_name"]),
                    "lat": float(r["stop_lat"]),
                    "lon": float(r["stop_lon"]),
                }
            )
    unknown = station_ids - {s["station_id"] for s in stations}
    if unknown:
        raise FeedValidationError(f"Regional stop_times use unknown stops: {sorted(unknown)[:5]}")

    shape_ids = {t["shape_id"] for t in trips.values() if t["shape_id"]}
    points = defaultdict(list)
    if "shapes.txt" in names:
        for r in _rows(archive, "shapes.txt"):
            if r["shape_id"] in shape_ids:
                point = (float(r["shape_pt_lon"]), float(r["shape_pt_lat"]))
                points[r["shape_id"]].append((int(r["shape_pt_sequence"]), *point))
    shapes = {
        shape_id: [[lon, lat] for _, lon, lat in sorted(pts)]
        for shape_id, pts in points.items()
        if len(pts) >= 2
    }
    for trip in trips.values():
        if trip["shape_id"] not in shapes:
            trip["shape_id"] = None

    service_ids = {t["service_id"] for t in trips.values()}
    dates = set()
    if "calendar.txt" in names:
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        for r in _rows(archive, "calendar.txt"):
            if r["service_id"] not in service_ids:
                continue
            day, end = gtfs_date(r["start_date"]), gtfs_date(r["end_date"])
            while day <= end:
                if r[weekdays[day.weekday()]] == "1":
                    dates.add((r["service_id"], day))
                day = date.fromordinal(day.toordinal() + 1)
    if "calendar_dates.txt" in names:
        for r in _rows(archive, "calendar_dates.txt"):
            if r["service_id"] not in service_ids:
                continue
            key = (r["service_id"], gtfs_date(r["date"]))
            if r["exception_type"] == "1":
                dates.add(key)
            else:
                dates.discard(key)

    info = next(_rows(archive, "feed_info.txt"), {}) if "feed_info.txt" in names else {}
    days = [d for _, d in dates]
    return {
        "feed_version": hashlib.sha256(zip_bytes).hexdigest(),
        "published_version": info.get("feed_version") or None,
        "service_start": min(days, default=None),
        "service_end": max(days, default=None),
        "lines": lines,
        "stations": stations,
        "shapes": shapes,
        "service_dates": sorted(dates),
        "trips": list(trips.values()),
        "stop_times": stop_times,
    }


def load(conn, feed: dict, source_url: str, loaded_at: datetime) -> bool:
    """Insert the version in one transaction; False if it was already loaded."""
    version = feed["feed_version"]
    with conn.transaction():
        inserted = conn.execute(
            """INSERT INTO normalized.regional_versions
               (feed_version, published_version, service_start, service_end, source_url, loaded_at)
               VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING feed_version""",
            (
                version,
                feed["published_version"],
                feed["service_start"],
                feed["service_end"],
                source_url,
                loaded_at,
            ),
        ).fetchone()
        if not inserted:
            return False
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO normalized.regional_lines
                   (feed_version, line_id, code, name, colour, text_colour)
                   VALUES (%(v)s, %(line_id)s, %(code)s, %(name)s, %(colour)s, %(text_colour)s)""",
                [{**line, "v": version} for line in feed["lines"]],
            )
            cur.executemany(
                """INSERT INTO normalized.regional_stations
                   (feed_version, station_id, name, lat, lon)
                   VALUES (%(v)s, %(station_id)s, %(name)s, %(lat)s, %(lon)s)""",
                [{**s, "v": version} for s in feed["stations"]],
            )
            cur.executemany(
                "INSERT INTO normalized.regional_shapes (feed_version, shape_id, path)"
                " VALUES (%s, %s, %s)",
                [(version, k, Jsonb(v)) for k, v in feed["shapes"].items()],
            )
            cur.executemany(
                "INSERT INTO normalized.regional_service_dates VALUES (%s, %s, %s)",
                [(version, service_id, day) for service_id, day in feed["service_dates"]],
            )
            cur.executemany(
                """INSERT INTO normalized.regional_trips
                   (feed_version, trip_id, line_id, service_id, direction_id, destination, shape_id)
                   VALUES (%(v)s, %(trip_id)s, %(line_id)s, %(service_id)s, %(direction_id)s,
                           %(destination)s, %(shape_id)s)""",
                [{**t, "v": version} for t in feed["trips"]],
            )
            with cur.copy(
                """COPY normalized.regional_stop_times (feed_version, trip_id, stop_sequence,
                   station_id, arrival_s, departure_s) FROM STDIN"""
            ) as copy:
                for row in feed["stop_times"]:
                    copy.write_row((version, *row))
    return True


def prune(conn, today: date) -> int:
    """Delete schedule versions whose service has ended, keeping the newest. Nothing
    historical joins against the regional schedule, and each version is ~70 MB. Stop times
    go first: they reference both a version's trips and its stations, so a cascade from the
    version alone could reach the stations before the stop times."""
    with conn.transaction():
        ended = [
            r["feed_version"]
            for r in conn.execute(
                """SELECT feed_version FROM normalized.regional_versions
                   WHERE service_end < %s
                     AND feed_version <> (SELECT feed_version FROM normalized.regional_versions
                                          ORDER BY loaded_at DESC LIMIT 1)""",
                (today,),
            )
        ]
        conn.execute(
            "DELETE FROM normalized.regional_stop_times WHERE feed_version = ANY(%s)", (ended,)
        )
        conn.execute(
            "DELETE FROM normalized.regional_versions WHERE feed_version = ANY(%s)", (ended,)
        )
    return len(ended)


def refresh(database_url: str, client: FeedClient, url: str = GTFS_URL) -> dict:
    started = datetime.now(UTC)
    with connect(database_url) as conn:
        try:
            payload = client.fetch_bytes(url, host=HOST, max_bytes=100 * 1024 * 1024)
            feed = parse(payload)
            loaded = load(conn, feed, url, started)
            result = {
                "status": "loaded" if loaded else "unchanged",
                "feed_version": feed["feed_version"],
                "lines": len(feed["lines"]),
                "stations": len(feed["stations"]),
                "trips": len(feed["trips"]),
                "pruned_versions": prune(conn, started.astimezone(TORONTO).date()),
            }
        except (FetchError, FeedValidationError) as exc:
            result = {"status": "failed", "feed_version": None, "error": str(exc)[:1000]}
        conn.execute(
            """INSERT INTO ops.regional_refreshes
               (started_at, finished_at, status, feed_version, error)
               VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], result["feed_version"], result.get("error")),
        )
    return result
