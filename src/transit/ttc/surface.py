"""Buses and streetcars from TTC static GTFS: routes, stops, and stop patterns with
scheduled travel times, stored under the same feed version as the subway schedule.

The site estimates arrivals from a vehicle's position and the scheduled time between
stops on its pattern, where TTC's own predictions (feed trips_surface) have none.
stop_times.txt has millions of rows; it is streamed trip by trip and only running sums
per pattern are kept, never the whole file.
"""

import dataclasses
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from io import BytesIO

from transit.bikeshare.parsing import FeedValidationError
from transit.ttc.static_gtfs import _rows, seconds

MODES = {"3": "bus", "0": "streetcar"}


@dataclass
class SurfaceFeed:
    routes: list[dict]
    stops: list[dict]
    patterns: list[dict]
    pattern_stops: list[tuple]
    # (pattern_id, service_id, start_hour, trips): scheduled trips by first departure hour.
    starts: list[tuple] = field(default_factory=list)
    trips: int = 0
    unpatterned_trips: int = 0
    stats: dict = field(default_factory=dict)


def towards(headsign: str) -> str:
    """'East - 504A King towards Distillery Loop' -> 'Distillery Loop'."""
    text = re.sub(r"^.*?\btowards\b\s*", "", headsign).strip()
    return text or headsign.strip()


def parse(zip_bytes: bytes, modes: dict = MODES, name_towards=towards) -> SurfaceFeed:
    """`modes` maps the route types kept to a mode; `name_towards` turns a trip headsign
    into a destination. Neighbouring agencies (transit.agencies) pass their own."""
    archive = zipfile.ZipFile(BytesIO(zip_bytes))
    routes = [
        {
            "route_id": r["route_id"],
            "short_name": r["route_short_name"],
            "long_name": r["route_long_name"],
            "mode": modes[r["route_type"]],
        }
        for r in _rows(archive, "routes.txt")
        if r["route_type"] in modes
    ]
    route_ids = {r["route_id"] for r in routes}
    if not route_ids:
        raise FeedValidationError("Static GTFS contains no bus or streetcar routes")
    trip_info = {
        r["trip_id"]: (
            r["route_id"],
            int(r["direction_id"]),
            r.get("trip_headsign") or "",
            r["service_id"],
        )
        for r in _rows(archive, "trips.txt")
        if r["route_id"] in route_ids
    }

    pattern_ids: dict[tuple, int] = {}
    pattern_meta: dict[int, dict] = {}
    offset_sums: dict[int, list[int]] = {}
    headsigns: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    starts: dict[tuple, int] = defaultdict(int)
    trip_count = 0
    finished: set[str] = set()
    skipped = 0

    def finish(trip_id, rows):
        nonlocal skipped, trip_count
        finished.add(trip_id)
        rows.sort()
        stops = tuple(stop for _, stop, _ in rows)
        times = [t for _, _, t in rows]
        if len(stops) < 2 or any(t is None for t in times):
            skipped += 1
            return
        route_id, direction_id, headsign, service_id = trip_info[trip_id]
        key = (route_id, direction_id, stops)
        pattern_id = pattern_ids.get(key)
        if pattern_id is None:
            pattern_id = len(pattern_ids) + 1
            pattern_ids[key] = pattern_id
            pattern_meta[pattern_id] = {
                "route_id": route_id,
                "direction_id": direction_id,
                "trips": 0,
            }
            offset_sums[pattern_id] = [0] * len(stops)
        pattern_meta[pattern_id]["trips"] += 1
        sums = offset_sums[pattern_id]
        for i, t in enumerate(times):
            sums[i] += t - times[0]
        headsigns[pattern_id][headsign] += 1
        starts[(pattern_id, service_id, times[0] // 3600)] += 1
        trip_count += 1

    # Rows arrive grouped by trip; a trip seen again later is merged before finishing.
    open_trips: dict[str, list] = {}
    current = None
    for r in _rows(archive, "stop_times.txt"):
        trip_id = r["trip_id"]
        if trip_id not in trip_info:
            continue
        if trip_id in finished:
            # Its rows were not contiguous and it was already patterned without these.
            raise FeedValidationError(f"stop_times.txt rows for trip {trip_id} are not grouped")
        if trip_id != current and current is not None and len(open_trips) > 64:
            for done in [t for t in open_trips if t != trip_id]:
                finish(done, open_trips.pop(done))
        current = trip_id
        when = r["arrival_time"] or r["departure_time"]
        open_trips.setdefault(trip_id, []).append(
            (int(r["stop_sequence"]), r["stop_id"], seconds(when) if when else None)
        )
    for trip_id, rows in open_trips.items():
        finish(trip_id, rows)

    patterns, pattern_stops = [], []
    for (_, _, stops), pattern_id in pattern_ids.items():
        meta = pattern_meta[pattern_id]
        headsign = max(headsigns[pattern_id].items(), key=lambda kv: kv[1])[0]
        patterns.append({**meta, "pattern_id": pattern_id, "towards": name_towards(headsign)})
        sums = offset_sums[pattern_id]
        for order, stop_id in enumerate(stops):
            pattern_stops.append((pattern_id, order, stop_id, round(sums[order] / meta["trips"])))

    used = {row[2] for row in pattern_stops}
    stops = [
        {
            "stop_id": r["stop_id"],
            "stop_code": r.get("stop_code") or None,
            "name": r["stop_name"].strip(),
            "lat": float(r["stop_lat"]),
            "lon": float(r["stop_lon"]),
        }
        for r in _rows(archive, "stops.txt")
        if r["stop_id"] in used
    ]
    if len(stops) != len(used):
        raise FeedValidationError(
            f"{len(used) - len(stops)} surface stops are missing from stops.txt"
        )
    return SurfaceFeed(
        routes=routes,
        stops=stops,
        patterns=patterns,
        pattern_stops=pattern_stops,
        starts=[(*key, trips) for key, trips in sorted(starts.items())],
        trips=trip_count,
        unpatterned_trips=skipped,
        stats={
            "routes": len(routes),
            "stops": len(stops),
            "patterns": len(patterns),
            "trips": trip_count,
        },
    )


def without_routes(feed: SurfaceFeed, route_ids) -> SurfaceFeed:
    """The network without the given routes: the light rail lines, which TTC's GTFS types
    as streetcars but which are loaded with the subway (transit.ttc.static_gtfs). Pattern
    IDs are kept, so they still match a re-parse of the same zip."""
    dropped = {p["pattern_id"] for p in feed.patterns if p["route_id"] in route_ids}
    routes = [r for r in feed.routes if r["route_id"] not in route_ids]
    patterns = [p for p in feed.patterns if p["pattern_id"] not in dropped]
    pattern_stops = [row for row in feed.pattern_stops if row[0] not in dropped]
    used = {row[2] for row in pattern_stops}
    stops = [s for s in feed.stops if s["stop_id"] in used]
    return dataclasses.replace(
        feed,
        routes=routes,
        stops=stops,
        patterns=patterns,
        pattern_stops=pattern_stops,
        starts=[row for row in feed.starts if row[0] not in dropped],
        stats={**feed.stats, "routes": len(routes), "stops": len(stops), "patterns": len(patterns)},
    )


def remove_routes(conn, feed_version: str, route_ids) -> list[str]:
    """Remove routes from a version loaded before they were excluded, with their patterns
    and the stops no other route serves. Returns the route IDs removed."""
    ids = sorted(route_ids)
    with conn.transaction(), conn.cursor() as cur:
        removed = [
            r["route_id"]
            for r in cur.execute(
                """DELETE FROM normalized.ttc_surface_routes
                   WHERE feed_version = %s AND route_id = ANY(%s) RETURNING route_id""",
                (feed_version, ids),
            ).fetchall()
        ]
        if not removed:
            return []
        cur.execute(
            """DELETE FROM normalized.ttc_surface_pattern_starts
               WHERE feed_version = %(v)s AND pattern_id IN (
                 SELECT pattern_id FROM normalized.ttc_surface_patterns
                 WHERE feed_version = %(v)s AND route_id = ANY(%(r)s))""",
            {"v": feed_version, "r": ids},
        )
        cur.execute(  # Cascades to the pattern stops.
            """DELETE FROM normalized.ttc_surface_patterns
               WHERE feed_version = %s AND route_id = ANY(%s)""",
            (feed_version, ids),
        )
        cur.execute(
            """DELETE FROM normalized.ttc_surface_stops s
               WHERE feed_version = %s AND NOT EXISTS (
                 SELECT 1 FROM normalized.ttc_surface_pattern_stops ps
                 WHERE ps.feed_version = s.feed_version AND ps.stop_id = s.stop_id)""",
            (feed_version,),
        )
    return sorted(removed, key=int)


def loaded(conn, feed_version: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM normalized.ttc_surface_routes WHERE feed_version = %s LIMIT 1",
            (feed_version,),
        ).fetchone()
    )


def load(conn, feed: SurfaceFeed, feed_version: str) -> dict:
    """Insert the surface network for a feed version in one transaction (once)."""
    with conn.transaction(), conn.cursor() as cur:
        if loaded(conn, feed_version):
            return {"status": "unchanged", **feed.stats}
        cur.executemany(
            """INSERT INTO normalized.ttc_surface_routes
               (feed_version, route_id, short_name, long_name, mode) VALUES (%s,%s,%s,%s,%s)""",
            [
                (feed_version, r["route_id"], r["short_name"], r["long_name"], r["mode"])
                for r in feed.routes
            ],
        )
        cur.executemany(
            """INSERT INTO normalized.ttc_surface_stops
               (feed_version, stop_id, stop_code, name, lat, lon) VALUES (%s,%s,%s,%s,%s,%s)""",
            [
                (feed_version, s["stop_id"], s["stop_code"], s["name"], s["lat"], s["lon"])
                for s in feed.stops
            ],
        )
        cur.executemany(
            """INSERT INTO normalized.ttc_surface_patterns
               (feed_version, pattern_id, route_id, direction_id, towards, trips)
               VALUES (%s,%s,%s,%s,%s,%s)""",
            [
                (
                    feed_version,
                    p["pattern_id"],
                    p["route_id"],
                    p["direction_id"],
                    p["towards"],
                    p["trips"],
                )
                for p in feed.patterns
            ],
        )
        with cur.copy(
            """COPY normalized.ttc_surface_pattern_stops
               (feed_version, pattern_id, stop_order, stop_id, offset_seconds) FROM STDIN"""
        ) as copy:
            for row in feed.pattern_stops:
                copy.write_row((feed_version, *row))
        _copy_starts(cur, feed, feed_version)
    return {"status": "loaded", **feed.stats}


def starts_loaded(conn, feed_version: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM normalized.ttc_surface_pattern_starts WHERE feed_version = %s LIMIT 1",
            (feed_version,),
        ).fetchone()
    )


def _copy_starts(cur, feed: SurfaceFeed, feed_version: str) -> None:
    with cur.copy(
        """COPY normalized.ttc_surface_pattern_starts
           (feed_version, pattern_id, service_id, start_hour, trips) FROM STDIN"""
    ) as copy:
        for row in feed.starts:
            copy.write_row((feed_version, *row))


def load_starts(conn, feed: SurfaceFeed, feed_version: str) -> int:
    """Scheduled trip starts for a version whose network was loaded before they were
    collected. Pattern IDs are assigned in file order, so a re-parse of the same zip
    gives the same IDs."""
    with conn.transaction(), conn.cursor() as cur:
        if starts_loaded(conn, feed_version):
            return 0
        _copy_starts(cur, feed, feed_version)
    return len(feed.starts)
