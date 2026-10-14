"""Parse the subway subset of TTC static GTFS and load it as an immutable feed version.

Every row is keyed by feed_version (the zip's SHA-256), so a new schedule never rewrites
the rows that historical observations were joined against.
"""

import csv
import hashlib
import io
import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from transit.bikeshare.parsing import FeedValidationError
from transit.ttc.sources import SUBWAY_ROUTE_TYPE

# Interchange platforms that GTFS names differently on each line.
STATION_ALIASES = {"Bloor": "Bloor-Yonge", "Yonge": "Bloor-Yonge"}
REQUIRED_FILES = ("routes.txt", "trips.txt", "stops.txt", "stop_times.txt")


@dataclass
class StaticFeed:
    feed_version: str
    routes: list[dict]
    stops: list[dict]
    trips: list[dict]
    stop_times: list[tuple]
    line_stops: list[dict]
    calendar: list[dict] = field(default_factory=list)
    calendar_dates: list[dict] = field(default_factory=list)
    service_start: date | None = None
    service_end: date | None = None


def station_parts(stop_name: str) -> tuple[str, str]:
    """'Union Station - Northbound Platform Towards X' -> ('Union', 'Northbound towards X')."""
    name, sep, platform = stop_name.partition(" Station - ")
    if not sep:
        # e.g. "York University - Northbound Platform" omits the word Station.
        name, sep, platform = stop_name.partition(" - ")
    if not sep:
        return stop_name.strip(), ""
    platform = platform.replace(" Platform", "").replace(" Towards ", " towards ").strip()
    name = name.strip()
    return STATION_ALIASES.get(name, name), platform


def station_key(station_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", station_name.lower().replace("'", "")).strip("-")


def seconds(value: str) -> int:
    """GTFS times may exceed 24:00:00 for service after midnight."""
    hours, minutes, secs = (int(part) for part in value.split(":"))
    return hours * 3600 + minutes * 60 + secs


def gtfs_date(value: str) -> date:
    return datetime.strptime(value, "%Y%m%d").date()


def _rows(archive: zipfile.ZipFile, name: str):
    with archive.open(name) as handle:
        yield from csv.DictReader(io.TextIOWrapper(handle, encoding="utf-8-sig", newline=""))


def parse(zip_bytes: bytes) -> StaticFeed:
    try:
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise FeedValidationError("Static GTFS is not a valid zip archive") from exc
    missing = [name for name in REQUIRED_FILES if name not in archive.namelist()]
    if missing:
        raise FeedValidationError(f"Static GTFS is missing {', '.join(missing)}")

    routes = [
        {
            "route_id": r["route_id"],
            "short_name": r["route_short_name"],
            "long_name": r["route_long_name"],
            "color": r.get("route_color") or None,
            "text_color": r.get("route_text_color") or None,
        }
        for r in _rows(archive, "routes.txt")
        if r["route_type"] == SUBWAY_ROUTE_TYPE
    ]
    route_ids = {r["route_id"] for r in routes}
    if not route_ids:
        raise FeedValidationError("Static GTFS contains no subway routes")

    trips = [
        {
            "trip_id": r["trip_id"],
            "route_id": r["route_id"],
            "service_id": r["service_id"],
            "direction_id": int(r["direction_id"]),
            "headsign": r.get("trip_headsign") or "",
        }
        for r in _rows(archive, "trips.txt")
        if r["route_id"] in route_ids
    ]
    trip_lookup = {t["trip_id"]: t for t in trips}

    stop_times = []
    sequences = defaultdict(list)
    for r in _rows(archive, "stop_times.txt"):
        if r["trip_id"] not in trip_lookup:
            continue
        sequence = int(r["stop_sequence"])
        stop_times.append(
            (
                r["trip_id"],
                sequence,
                r["stop_id"],
                seconds(r["arrival_time"]) if r["arrival_time"] else None,
                seconds(r["departure_time"]) if r["departure_time"] else None,
            )
        )
        sequences[r["trip_id"]].append((sequence, r["stop_id"]))
    if not stop_times:
        raise FeedValidationError("Static GTFS has no subway stop times")

    # The most common full stop pattern per line/direction defines display order.
    patterns = Counter()
    headsigns = defaultdict(Counter)
    for trip_id, stops in sequences.items():
        trip = trip_lookup[trip_id]
        key = (trip["route_id"], trip["direction_id"])
        patterns[(key, tuple(stop for _, stop in sorted(stops)))] += 1
        headsigns[key][trip["headsign"]] += 1
    canonical = {}
    for (key, pattern), _count in patterns.most_common():
        canonical.setdefault(key, pattern)
    line_stops = []
    for (route_id, direction_id), pattern in sorted(canonical.items()):
        headsign = headsigns[(route_id, direction_id)].most_common(1)[0][0]
        towards = re.sub(r"^.*?\btowards\b\s*", "", headsign).removesuffix(" Station")
        for order, stop_id in enumerate(pattern, start=1):
            line_stops.append(
                {
                    "route_id": route_id,
                    "direction_id": direction_id,
                    "stop_order": order,
                    "stop_id": stop_id,
                    "towards": towards,
                }
            )
    placed = {row["stop_id"] for row in line_stops}
    if len(placed) != len(line_stops):
        raise FeedValidationError("A subway platform appears in more than one line direction")
    used_stops = {row[2] for row in stop_times}
    if not used_stops <= placed:
        raise FeedValidationError(
            f"{len(used_stops - placed)} subway platforms are outside every canonical pattern"
        )
    stop_route = {row["stop_id"]: row["route_id"] for row in line_stops}
    if any(stop_route[row[2]] != trip_lookup[row[0]]["route_id"] for row in stop_times):
        raise FeedValidationError("A subway trip stops at a platform of another line")

    stops = []
    for r in _rows(archive, "stops.txt"):
        if r["stop_id"] not in placed:
            continue
        name, platform = station_parts(r["stop_name"])
        stops.append(
            {
                "stop_id": r["stop_id"],
                "stop_name": r["stop_name"],
                "station_name": name,
                "station_key": station_key(name),
                "platform": platform,
                "lat": float(r["stop_lat"]),
                "lon": float(r["stop_lon"]),
                "wheelchair_boarding": int(r["wheelchair_boarding"] or 0),
            }
        )
    if {s["stop_id"] for s in stops} != placed:
        raise FeedValidationError("Some subway platforms are missing from stops.txt")

    calendar, calendar_dates = [], []
    if "calendar.txt" in archive.namelist():
        days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        calendar = [
            {
                "service_id": r["service_id"],
                "days": [r[d] == "1" for d in days],
                "start_date": gtfs_date(r["start_date"]),
                "end_date": gtfs_date(r["end_date"]),
            }
            for r in _rows(archive, "calendar.txt")
        ]
    if "calendar_dates.txt" in archive.namelist():
        calendar_dates = [
            {
                "service_id": r["service_id"],
                "date": gtfs_date(r["date"]),
                "exception_type": int(r["exception_type"]),
            }
            for r in _rows(archive, "calendar_dates.txt")
        ]
    service_days = [c["start_date"] for c in calendar] + [c["end_date"] for c in calendar]
    service_days += [c["date"] for c in calendar_dates]
    return StaticFeed(
        feed_version=hashlib.sha256(zip_bytes).hexdigest(),
        routes=routes,
        stops=stops,
        trips=trips,
        stop_times=stop_times,
        line_stops=line_stops,
        calendar=calendar,
        calendar_dates=calendar_dates,
        service_start=min(service_days, default=None),
        service_end=max(service_days, default=None),
    )


def load(conn, feed: StaticFeed, source_url: str, downloaded_at) -> bool:
    """Insert a new feed version atomically; returns False if it was already loaded."""
    with conn.transaction():
        inserted = conn.execute(
            """INSERT INTO normalized.ttc_gtfs_versions
               (feed_version, source_url, downloaded_at, service_start, service_end,
                route_count, stop_count, trip_count, stop_time_count)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (feed_version) DO NOTHING RETURNING feed_version""",
            (
                feed.feed_version,
                source_url,
                downloaded_at,
                feed.service_start,
                feed.service_end,
                len(feed.routes),
                len(feed.stops),
                len(feed.trips),
                len(feed.stop_times),
            ),
        ).fetchone()
        if not inserted:
            return False
        v = feed.feed_version
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO normalized.ttc_routes
                   (feed_version, route_id, short_name, long_name, color, text_color)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                [
                    (v, r["route_id"], r["short_name"], r["long_name"], r["color"], r["text_color"])
                    for r in feed.routes
                ],
            )
            cur.executemany(
                """INSERT INTO normalized.ttc_stops
                   (feed_version, stop_id, stop_name, station_name, station_key, platform,
                    lat, lon, wheelchair_boarding)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        v,
                        s["stop_id"],
                        s["stop_name"],
                        s["station_name"],
                        s["station_key"],
                        s["platform"],
                        s["lat"],
                        s["lon"],
                        s["wheelchair_boarding"],
                    )
                    for s in feed.stops
                ],
            )
            cur.executemany(
                """INSERT INTO normalized.ttc_line_stops
                   (feed_version, route_id, direction_id, stop_order, stop_id, towards)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        v,
                        r["route_id"],
                        r["direction_id"],
                        r["stop_order"],
                        r["stop_id"],
                        r["towards"],
                    )
                    for r in feed.line_stops
                ],
            )
            cur.executemany(
                """INSERT INTO normalized.ttc_calendar
                   (feed_version, service_id, monday, tuesday, wednesday, thursday, friday,
                    saturday, sunday, start_date, end_date)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    (v, c["service_id"], *c["days"], c["start_date"], c["end_date"])
                    for c in feed.calendar
                ],
            )
            cur.executemany(
                """INSERT INTO normalized.ttc_calendar_dates
                   (feed_version, service_id, date, exception_type) VALUES (%s,%s,%s,%s)""",
                [(v, c["service_id"], c["date"], c["exception_type"]) for c in feed.calendar_dates],
            )
            with cur.copy(
                """COPY normalized.ttc_trips
                   (feed_version, trip_id, route_id, service_id, direction_id, headsign)
                   FROM STDIN"""
            ) as copy:
                for t in feed.trips:
                    copy.write_row(
                        (
                            v,
                            t["trip_id"],
                            t["route_id"],
                            t["service_id"],
                            t["direction_id"],
                            t["headsign"],
                        )
                    )
            with cur.copy(
                """COPY normalized.ttc_stop_times
                   (feed_version, trip_id, stop_sequence, stop_id, arrival_seconds,
                    departure_seconds) FROM STDIN"""
            ) as copy:
                for row in feed.stop_times:
                    copy.write_row((v, *row))
    return True
