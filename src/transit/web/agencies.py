"""Read queries for neighbouring bus agencies (transit.agencies), in the same shapes as the
TTC's buses (transit.web.surface) so the map shows them the same way.

Every ID the site sends is prefixed with the agency's key ("br:511"), because route and
stop numbers repeat across agencies; the database keeps the agencies' own IDs. A route's
mode is its agency's key, which the map uses for its colour.
"""

import threading
from datetime import datetime

from transit.agencies import AGENCIES
from transit.ttc.matching import M_PER_DEG_LAT, M_PER_DEG_LON, index_rows, locate
from transit.web import surface

_index_lock = threading.Lock()
_index: dict = {}


def split(prefixed: str) -> tuple[str, str] | None:
    """'br:511' -> ('br', '511'); None if the prefix is not a known agency."""
    agency, sep, local = prefixed.partition(":")
    return (agency, local) if sep and agency in AGENCIES and local else None


def versions(conn) -> dict[str, str]:
    return {
        r["agency"]: r["feed_version"]
        for r in conn.execute(
            """SELECT DISTINCT ON (agency) agency, feed_version FROM normalized.agency_versions
               ORDER BY agency, loaded_at DESC"""
        )
    }


def pattern_index(conn) -> dict[str, dict[str, list[dict]]]:
    """{agency: {route: [pattern]}}, as transit.ttc.matching builds for the TTC, with
    prefixed pattern IDs; rebuilt when an agency loads a new version."""
    current = versions(conn)
    with _index_lock:
        if _index.get("versions") == current:
            return _index["agencies"]
        out = {}
        for agency, version in current.items():
            rows = conn.execute(
                """WITH p AS (
                     SELECT *, trips::float / sum(trips) OVER (PARTITION BY route_id, direction_id)
                               AS share
                     FROM normalized.agency_patterns WHERE agency = %(a)s AND feed_version = %(v)s
                   )
                   SELECT p.pattern_id, p.route_id, p.direction_id, p.towards, p.trips,
                          ps.stop_id, ps.offset_seconds, s.lat, s.lon
                   FROM p
                   JOIN normalized.agency_pattern_stops ps
                     ON ps.agency = p.agency AND ps.feed_version = p.feed_version
                    AND ps.pattern_id = p.pattern_id
                   JOIN normalized.agency_stops s
                     ON s.agency = ps.agency AND s.feed_version = ps.feed_version
                    AND s.stop_id = ps.stop_id
                   WHERE p.share >= 0.03
                   ORDER BY p.pattern_id, ps.stop_order""",
                {"a": agency, "v": version},
            ).fetchall()
            routes = index_rows(rows)
            for patterns in routes.values():
                for pattern in patterns:
                    pattern["pattern_id"] = f"{agency}:{pattern['pattern_id']}"
            out[agency] = routes
        _index.clear()
        _index.update(versions=current, agencies=out)
        return out


def network(conn) -> dict:
    """Every agency route and stop, in /api/surface's shape."""
    routes = conn.execute(
        """SELECT r.agency, r.route_id, r.short_name, r.long_name
           FROM normalized.agency_routes r
           JOIN normalized.agency_versions v USING (agency, feed_version)
           ORDER BY r.agency, length(r.short_name), r.short_name"""
    ).fetchall()
    stops = conn.execute(
        """SELECT s.agency, s.stop_id, s.stop_code, s.name,
                  round(s.lat::numeric, 5)::float AS lat, round(s.lon::numeric, 5)::float AS lon,
                  string_agg(DISTINCT p.route_id, ',') AS routes
           FROM normalized.agency_stops s
           JOIN normalized.agency_pattern_stops ps USING (agency, feed_version, stop_id)
           JOIN normalized.agency_patterns p USING (agency, feed_version, pattern_id)
           GROUP BY s.agency, s.stop_id, s.stop_code, s.name, s.lat, s.lon"""
    ).fetchall()
    return {
        "routes": [
            {
                "route_id": f"{r['agency']}:{r['route_id']}",
                "short_name": r["short_name"],
                "long_name": r["long_name"],
                "mode": r["agency"],
                "agency": AGENCIES[r["agency"]].name,
            }
            for r in routes
            if r["agency"] in AGENCIES
        ],
        "stops": [
            [
                f"{s['agency']}:{s['stop_id']}",
                s["stop_code"],
                s["name"],
                s["lat"],
                s["lon"],
                [f"{s['agency']}:{r}" for r in s["routes"].split(",")],
            ]
            for s in stops
            if s["agency"] in AGENCIES
        ],  # fmt: skip
    }


def matched_vehicles(conn, now: datetime) -> list[dict]:
    """Live buses matched to their route's patterns, in surface.matched_vehicles' shape
    with prefixed IDs; the agency's own stop IDs stay in each pattern."""
    index = pattern_index(conn)
    rows = conn.execute(
        """SELECT agency, vehicle_id, route_id, lat, lon, bearing, speed_mps, occupancy,
                  reported_at
           FROM normalized.agency_vehicles WHERE reported_at > %s
           ORDER BY agency, vehicle_id""",
        (now - surface.VEHICLE_FRESH,),
    ).fetchall()
    out = []
    for r in rows:
        if r["agency"] not in AGENCIES:
            continue
        patterns = index.get(r["agency"], {}).get(r["route_id"], [])
        match = locate(r["lat"], r["lon"], r["bearing"], patterns)
        out.append(
            {
                **r,
                "vehicle_id": f"{r['agency']}:{r['vehicle_id']}",
                "route_id": f"{r['agency']}:{r['route_id']}",
                "mode": r["agency"],
                "pattern": match[0] if match else None,
                "position": match[1] if match else None,
            }
        )
    return out


def patterns(conn) -> dict:
    """Every agency pattern, in /api/patterns' shape."""
    return {
        p["pattern_id"]: [
            [round(x / M_PER_DEG_LON, 5), round(y / M_PER_DEG_LAT, 5), offset]
            for (x, y), offset in zip(p["xy"], p["offsets"], strict=True)
        ]
        for routes in pattern_index(conn).values()
        for ps in routes.values()
        for p in ps
    }


def stop(conn, prefixed: str, matched: list[dict], now: datetime) -> dict | None:
    parts = split(prefixed)
    if parts is None:
        return None
    agency, stop_id = parts
    version = versions(conn).get(agency)
    info = conn.execute(
        """SELECT stop_id, stop_code, name, lat, lon FROM normalized.agency_stops
           WHERE agency = %s AND feed_version = %s AND stop_id = %s""",
        (agency, version, stop_id),
    ).fetchone()
    if info is None:
        return None
    routes = conn.execute(
        """SELECT DISTINCT r.route_id, r.short_name, r.long_name, p.towards
           FROM normalized.agency_pattern_stops ps
           JOIN normalized.agency_patterns p USING (agency, feed_version, pattern_id)
           JOIN normalized.agency_routes r USING (agency, feed_version, route_id)
           WHERE ps.agency = %s AND ps.feed_version = %s AND ps.stop_id = %s
           ORDER BY r.short_name, p.towards""",
        (agency, version, stop_id),
    ).fetchall()
    name = AGENCIES[agency].name
    routes = [
        {**r, "route_id": f"{agency}:{r['route_id']}", "mode": agency, "agency": name}
        for r in routes
    ]
    own = [v for v in matched if v["mode"] == agency]
    return {
        "stop": {
            "id": prefixed,
            "code": info["stop_code"],
            "name": info["name"],
            "lat": info["lat"],
            "lon": info["lon"],
            "agency": AGENCIES[agency].name,
        },
        "routes": routes,
        "arrivals": surface.estimate_arrivals(
            own, stop_id, {r["route_id"]: r["short_name"] for r in routes}, now
        ),
    }


def route(conn, prefixed: str) -> dict | None:
    parts = split(prefixed)
    if parts is None:
        return None
    agency, route_id = parts
    version = versions(conn).get(agency)
    meta = conn.execute(
        """SELECT route_id, short_name, long_name FROM normalized.agency_routes
           WHERE agency = %s AND feed_version = %s AND route_id = %s""",
        (agency, version, route_id),
    ).fetchone()
    if meta is None:
        return None
    rows = conn.execute(
        """SELECT pattern_id, direction_id, towards, trips,
                  trips::float / sum(trips) OVER (PARTITION BY direction_id) AS share
           FROM normalized.agency_patterns
           WHERE agency = %s AND feed_version = %s AND route_id = %s
           ORDER BY direction_id, trips DESC""",
        (agency, version, route_id),
    ).fetchall()
    shown = surface.main_patterns(rows)
    paths: dict = {}
    for r in conn.execute(
        """SELECT ps.pattern_id, round(s.lon::numeric, 5)::float AS lon,
                  round(s.lat::numeric, 5)::float AS lat
           FROM normalized.agency_pattern_stops ps
           JOIN normalized.agency_stops s USING (agency, feed_version, stop_id)
           WHERE ps.agency = %s AND ps.feed_version = %s AND ps.pattern_id = ANY(%s)
           ORDER BY ps.pattern_id, ps.stop_order""",
        (agency, version, [p["pattern_id"] for p in shown]),
    ):
        paths.setdefault(r["pattern_id"], []).append([r["lon"], r["lat"]])
    return {
        "route": {
            "route_id": prefixed,
            "short_name": meta["short_name"],
            "long_name": meta["long_name"],
            "mode": agency,
            "agency": AGENCIES[agency].name,
        },
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
