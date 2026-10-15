"""Matching bus and streetcar positions to their route's stop patterns, shared by the
site's arrival estimates and the collector's stop passages.

TTC's surface feed gives positions, not predictions, and neither its trip IDs nor its stop
IDs match the published schedule. So each vehicle is matched by geometry: it is snapped to
the nearest point on one of its route's stop patterns, skipping segments that run against
its heading, and left unmatched if every pattern is more than MATCH_DISTANCE_M away (a
detour, or on its way to a garage).
"""

import math
import threading

# Matching considers patterns with at least this share of their direction's trips.
MATCH_SHARE = 0.03
MATCH_DISTANCE_M = 120
BEARING_TOLERANCE = 100  # degrees between a vehicle's heading and a segment's direction
# Metres per degree around Toronto (latitude ~43.7), for an equirectangular projection.
M_PER_DEG_LAT = 111_130
M_PER_DEG_LON = 111_320 * math.cos(math.radians(43.7))
# Stop passages are recorded at about one stop per MEASURE_EVERY_SECONDS of scheduled
# travel along a pattern, not at all ~9,000 stops: enough for route headways, at about a
# tenth of the rows.
MEASURE_EVERY_SECONDS = 600

_index_lock = threading.Lock()
_index: dict = {}


def measured_stops(offsets: list[int]) -> list[int]:
    """Stop indexes where the scheduled offset enters a new MEASURE_EVERY_SECONDS block.
    The first and last stops are never measured: vehicles lay over and queue there, which
    showed up as headways of a few seconds. A pattern too short to cross a block gets its
    middle stop."""
    last = len(offsets) - 1
    out = [
        i
        for i in range(1, last)
        if offsets[i] // MEASURE_EVERY_SECONDS > offsets[i - 1] // MEASURE_EVERY_SECONDS
    ]
    if not out and last > 0:
        out = [max(1, last // 2)]
    return out


def _xy(lat: float, lon: float) -> tuple[float, float]:
    return lon * M_PER_DEG_LON, lat * M_PER_DEG_LAT


def pattern_index(conn, version: str) -> dict[str, list[dict]]:
    """Each route's main patterns with projected stop positions and scheduled offsets,
    built once per schedule version."""
    with _index_lock:
        if _index.get("version") == version and _index.get("routes"):
            return _index["routes"]
        rows = conn.execute(
            """WITH p AS (
                 SELECT *, trips::float / sum(trips) OVER (PARTITION BY route_id, direction_id)
                           AS share
                 FROM normalized.ttc_surface_patterns WHERE feed_version = %s
               )
               SELECT p.pattern_id, p.route_id, p.direction_id, p.towards, p.trips,
                      ps.stop_id,
                      ps.offset_seconds, s.lat, s.lon
               FROM p
               JOIN normalized.ttc_surface_pattern_stops ps
                 ON ps.feed_version = p.feed_version AND ps.pattern_id = p.pattern_id
               JOIN normalized.ttc_surface_stops s
                 ON s.feed_version = ps.feed_version AND s.stop_id = ps.stop_id
               WHERE p.share >= %s
               ORDER BY p.pattern_id, ps.stop_order""",
            (version, MATCH_SHARE),
        ).fetchall()
        routes = index_rows(rows)
        if routes:
            _index.clear()
            _index.update(version=version, routes=routes)
        return routes


def index_rows(rows) -> dict[str, list[dict]]:
    """{route: [pattern]} from rows of one pattern stop each, in stop order: pattern_id,
    route_id, direction_id, towards, trips, stop_id, offset_seconds, lat and lon."""
    patterns: dict = {}
    for r in rows:
        pattern = patterns.setdefault(
            r["pattern_id"],
            {
                "pattern_id": r["pattern_id"],
                "route": r["route_id"],
                "direction_id": r["direction_id"],
                "towards": r["towards"],
                "trips": r["trips"],
                "stops": [],
                "offsets": [],
                "xy": [],
            },
        )
        pattern["stops"].append(r["stop_id"])
        pattern["offsets"].append(r["offset_seconds"])
        pattern["xy"].append(_xy(r["lat"], r["lon"]))
    routes: dict[str, list[dict]] = {}
    for pattern in patterns.values():
        pattern["order"] = {}
        for i, stop_id in enumerate(pattern["stops"]):
            pattern["order"].setdefault(stop_id, i)
        pattern["measured"] = measured_stops(pattern["offsets"])
        routes.setdefault(pattern["route"], []).append(pattern)
    return routes


def _angle(a: float, b: float) -> float:
    return abs((a - b + 180) % 360 - 180)


def locate(lat: float, lon: float, bearing, patterns: list[dict]):
    """(pattern, position): position is a fractional stop index along the pattern nearest
    the vehicle; None if every pattern is farther than MATCH_DISTANCE_M."""
    x, y = _xy(lat, lon)
    best = None
    for pattern in patterns:
        xy = pattern["xy"]
        for i in range(len(xy) - 1):
            (ax, ay), (bx, by) = xy[i], xy[i + 1]
            dx, dy = bx - ax, by - ay
            length2 = dx * dx + dy * dy
            if length2 == 0:
                continue
            # The heading only means something on segments longer than 20 m.
            if bearing is not None and length2 > 400:
                heading = math.degrees(math.atan2(dx, dy)) % 360
                if _angle(heading, bearing) > BEARING_TOLERANCE:
                    continue
            f = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
            distance = math.hypot(x - (ax + f * dx), y - (ay + f * dy))
            key = (distance, -pattern["trips"])
            if best is None or key < best[0]:
                best = (key, pattern, i + f)
    if best is None or best[0][0] > MATCH_DISTANCE_M:
        return None
    return best[1], best[2]
