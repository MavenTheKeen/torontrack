"""Read queries for buses and streetcars: the stop and route network, live vehicles, and
arrivals estimated from vehicle positions.

TTC's surface feed gives positions, not predictions, and neither its trip IDs nor its
stop IDs match the published schedule. So each vehicle is matched by geometry: it is
snapped to the nearest point on one of its route's stop patterns, skipping segments that
run against its heading, and left unmatched if every pattern is more than
MATCH_DISTANCE_M away (a detour, or on its way to a garage). An arrival estimate is the
scheduled time along that pattern from the vehicle's point to the stop asked about, less
the age of its position report. Estimates are labelled as such and assume the schedule's
pace from here.
"""

from datetime import datetime, timedelta

from transit.ttc.matching import M_PER_DEG_LAT, M_PER_DEG_LON, locate, pattern_index
from transit.ttc.realtime import TORONTO

# Positions older than this are left off the map and out of estimates.
VEHICLE_FRESH = timedelta(minutes=5)
MAX_ESTIMATE = timedelta(minutes=60)
ARRIVALS_SHOWN = 10
# A route's drawn path uses its patterns that carry at least this share of the trips in
# their direction, so branches show but one-off detours do not.
PATTERN_SHARE = 0.1
PATTERNS_PER_DIRECTION = 3
# Recent days of stop passages a route's usual pace is measured over, and the range a pace
# is kept within (seconds of timetable per real second).
PACE_DAYS = 28
PACE_LIMITS = (0.2, 2.0)


def network(conn, version: str) -> dict:
    """Every surface route and stop, with the routes serving each stop (for the map)."""
    routes = conn.execute(
        """SELECT route_id, short_name, long_name, mode FROM normalized.ttc_surface_routes
           WHERE feed_version = %s ORDER BY length(short_name), short_name""",
        (version,),
    ).fetchall()
    stops = conn.execute(
        """SELECT s.stop_id, s.stop_code, s.name, round(s.lat::numeric, 5)::float AS lat,
                  round(s.lon::numeric, 5)::float AS lon,
                  string_agg(DISTINCT p.route_id, ',') AS routes
           FROM normalized.ttc_surface_stops s
           JOIN normalized.ttc_surface_pattern_stops ps USING (feed_version, stop_id)
           JOIN normalized.ttc_surface_patterns p USING (feed_version, pattern_id)
           WHERE s.feed_version = %s
           GROUP BY s.stop_id, s.stop_code, s.name, s.lat, s.lon""",
        (version,),
    ).fetchall()
    return {
        "routes": [dict(r) for r in routes],
        "stop_fields": ["id", "code", "name", "lat", "lon", "routes"],
        "stops": [
            [s["stop_id"], s["stop_code"], s["name"], s["lat"], s["lon"], s["routes"].split(",")]
            for s in stops
        ],
    }


def matched_vehicles(conn, version: str, now: datetime) -> list[dict]:
    routes = pattern_index(conn, version)
    rows = conn.execute(
        """SELECT v.vehicle_id, v.route_id, coalesce(r.mode, 'bus') AS mode, v.lat, v.lon,
                  v.bearing, v.speed_mps, v.occupancy, v.reported_at
           FROM normalized.ttc_vehicles v
           LEFT JOIN normalized.ttc_surface_routes r
             ON r.feed_version = %s AND r.route_id = v.route_id
           WHERE v.reported_at > %s ORDER BY v.vehicle_id""",
        (version, now - VEHICLE_FRESH),
    ).fetchall()
    out = []
    for r in rows:
        match = locate(r["lat"], r["lon"], r["bearing"], routes.get(r["route_id"], []))
        out.append(
            {
                **r,
                "pattern": match[0] if match else None,
                "position": match[1] if match else None,
            }
        )
    return out


def _offset_at(pattern: dict, position: float) -> float:
    offsets = pattern["offsets"]
    i = min(int(position), len(offsets) - 1)
    j = min(i + 1, len(offsets) - 1)
    return offsets[i] + (position - i) * (offsets[j] - offsets[i])


def typical_pace(conn, version: str, now: datetime) -> dict[tuple, float]:
    """{(route, direction, hour): pace}: how many seconds of timetable a route's vehicles
    cover per real second at that hour (Toronto time), from consecutive stop passages of
    the same vehicle on the same pattern over the last PACE_DAYS. Legs under a minute of
    timetable, and those taking over four times it (a layover or a detour), are left out.
    """
    rows = conn.execute(
        """WITH p AS (
             SELECT route_id, direction_id, pattern_id, stop_id, passed_at,
                    lag(stop_id) OVER w AS prev_stop, lag(passed_at) OVER w AS prev_at,
                    lag(pattern_id) OVER w AS prev_pattern
             FROM normalized.ttc_surface_passages
             WHERE passed_at > %(since)s
             WINDOW w AS (PARTITION BY vehicle_id ORDER BY passed_at)
           ), legs AS (
             SELECT p.route_id, p.direction_id,
                    extract(hour FROM p.passed_at AT TIME ZONE 'America/Toronto')::int AS hour,
                    extract(epoch FROM p.passed_at - p.prev_at) AS actual,
                    b.offset_seconds - a.offset_seconds AS scheduled
             FROM p
             JOIN normalized.ttc_surface_pattern_stops a
               ON a.feed_version = %(v)s AND a.pattern_id = p.pattern_id
              AND a.stop_id = p.prev_stop
             JOIN normalized.ttc_surface_pattern_stops b
               ON b.feed_version = %(v)s AND b.pattern_id = p.pattern_id
              AND b.stop_id = p.stop_id
             WHERE p.prev_pattern = p.pattern_id
           )
           SELECT route_id, direction_id, hour, sum(scheduled) / sum(actual) AS pace
           FROM legs
           WHERE scheduled > 60 AND actual > 0 AND actual < scheduled * 4
           GROUP BY route_id, direction_id, hour
           HAVING sum(actual) > 600""",
        {"since": now - timedelta(days=PACE_DAYS), "v": version},
    ).fetchall()
    return {(r["route_id"], r["direction_id"], r["hour"]): float(r["pace"]) for r in rows}


def patterns(conn, version: str) -> dict:
    """Every pattern vehicles are matched to, as [lon, lat, timetable seconds] per stop, so
    the browser can move a vehicle along its route between position reports."""
    return {
        "patterns": {
            p["pattern_id"]: [
                [round(x / M_PER_DEG_LON, 5), round(y / M_PER_DEG_LAT, 5), offset]
                for (x, y), offset in zip(p["xy"], p["offsets"], strict=True)
            ]
            for ps in pattern_index(conn, version).values()
            for p in ps
        }
    }


def vehicles(matched: list[dict], now: datetime, pace: dict | None = None) -> dict:
    """Every vehicle with, when matched to a pattern, what moving it needs (motion.js):
    the pattern, its fractional stop position, the report time (epoch seconds), whether it
    reported standing still, and its route's usual pace this hour (1.0 = timetable)."""
    newest = max((v["reported_at"] for v in matched), default=None)
    hour = now.astimezone(TORONTO).hour
    pace = pace or {}

    def motion(v):
        if not v["pattern"]:
            return [None, None, None]
        usual = pace.get((v["route_id"], v["pattern"]["direction_id"], hour), 1.0)
        return [
            v["pattern"]["pattern_id"],
            round(v["position"], 4),
            round(min(max(usual, PACE_LIMITS[0]), PACE_LIMITS[1]), 3),
        ]

    return {
        "as_of": newest,
        "stale": newest is None or now - newest > timedelta(minutes=2),
        "matched": sum(1 for v in matched if v["pattern"]),
        "fields": [
            "id",
            "route",
            "mode",
            "lat",
            "lon",
            "bearing",
            "towards",
            "occupancy",
            "reported",
            "stopped",
            "pattern",
            "position",
            "pace",
        ],  # fmt: skip
        "vehicles": [
            [
                v["vehicle_id"],
                v["route_id"],
                v["mode"],
                round(v["lat"], 5),
                round(v["lon"], 5),
                v["bearing"],
                v["pattern"]["towards"] if v["pattern"] else None,
                v["occupancy"],
                int(v["reported_at"].timestamp()),
                v.get("speed_mps") == 0,
                *motion(v),
            ]
            for v in matched
        ],
    }


def stop(conn, version: str, stop_id: str, matched: list[dict], now: datetime) -> dict | None:
    info = conn.execute(
        """SELECT stop_id, stop_code, name, lat, lon FROM normalized.ttc_surface_stops
           WHERE feed_version = %s AND stop_id = %s""",
        (version, stop_id),
    ).fetchone()
    if info is None:
        return None
    routes = conn.execute(
        """SELECT DISTINCT r.route_id, r.short_name, r.long_name, r.mode, p.towards
           FROM normalized.ttc_surface_pattern_stops ps
           JOIN normalized.ttc_surface_patterns p USING (feed_version, pattern_id)
           JOIN normalized.ttc_surface_routes r USING (feed_version, route_id)
           WHERE ps.feed_version = %s AND ps.stop_id = %s
           ORDER BY r.short_name, p.towards""",
        (version, stop_id),
    ).fetchall()
    short_names = {r["route_id"]: r["short_name"] for r in routes}
    return {
        "stop": {
            "id": info["stop_id"],
            "code": info["stop_code"],
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
        },
        "routes": [dict(r) for r in routes],
        "arrivals": estimate_arrivals(matched, stop_id, short_names, now),
    }


def estimate_arrivals(matched: list[dict], stop_id: str, short_names: dict, now: datetime):
    """The next vehicles due at a stop: scheduled time along each matched vehicle's
    pattern from its position to the stop, less the age of its report."""
    arrivals = []
    for v in matched:
        pattern = v["pattern"]
        order = pattern["order"].get(stop_id) if pattern else None
        if order is None or order < v["position"]:
            continue
        scheduled = pattern["offsets"][order] - _offset_at(pattern, v["position"])
        if scheduled > MAX_ESTIMATE.total_seconds():
            continue
        age = (now - v["reported_at"]).total_seconds()
        arrivals.append(
            {
                "vehicle_id": v["vehicle_id"],
                "route_id": v["route_id"],
                "short_name": short_names.get(v["route_id"], v["route_id"]),
                "mode": v["mode"],
                "towards": pattern["towards"],
                "occupancy": v["occupancy"],
                "seconds": max(0, round(scheduled - age)),
            }
        )
    arrivals.sort(key=lambda a: (a["seconds"], a["vehicle_id"]))
    return arrivals[:ARRIVALS_SHOWN]


def main_patterns(patterns: list[dict]) -> list[dict]:
    """The patterns a route's drawn path uses: by trips, at least PATTERN_SHARE of their
    direction's and at most PATTERNS_PER_DIRECTION per direction."""
    shown, per_direction = [], {}
    for p in patterns:
        count = per_direction.get(p["direction_id"], 0)
        if p["share"] >= PATTERN_SHARE and count < PATTERNS_PER_DIRECTION:
            per_direction[p["direction_id"]] = count + 1
            shown.append(p)
    return shown


def route(conn, version: str, route_id: str) -> dict | None:
    meta = conn.execute(
        """SELECT route_id, short_name, long_name, mode FROM normalized.ttc_surface_routes
           WHERE feed_version = %s AND route_id = %s""",
        (version, route_id),
    ).fetchone()
    if meta is None:
        return None
    patterns = conn.execute(
        """SELECT pattern_id, direction_id, towards, trips,
                  trips::float / sum(trips) OVER (PARTITION BY direction_id) AS share
           FROM normalized.ttc_surface_patterns
           WHERE feed_version = %s AND route_id = %s
           ORDER BY direction_id, trips DESC""",
        (version, route_id),
    ).fetchall()
    shown = main_patterns(patterns)
    paths = {}
    for r in conn.execute(
        """SELECT ps.pattern_id, round(s.lon::numeric, 5)::float AS lon,
                  round(s.lat::numeric, 5)::float AS lat
           FROM normalized.ttc_surface_pattern_stops ps
           JOIN normalized.ttc_surface_stops s USING (feed_version, stop_id)
           WHERE ps.feed_version = %s AND ps.pattern_id = ANY(%s)
           ORDER BY ps.pattern_id, ps.stop_order""",
        (version, [p["pattern_id"] for p in shown]),
    ):
        paths.setdefault(r["pattern_id"], []).append([r["lon"], r["lat"]])
    return {
        "route": dict(meta),
        "patterns": [
            {
                "direction_id": p["direction_id"],
                "towards": p["towards"],
                "trips": p["trips"],
                "path": paths.get(p["pattern_id"], []),
            }
            for p in shown
        ],
    }
