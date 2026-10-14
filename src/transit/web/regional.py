"""Read queries for regional trains: lines, stations, and train positions and
departures from the timetable.

Metrolinx publishes live train data only to registered API key holders, so positions here
are where each train should be by its schedule: at a station while it dwells there, and in
between interpolated along the track by time. Every response says so; nothing here claims
a train is running late or on time.
"""

import math
import threading
from bisect import bisect_right
from datetime import date, datetime, time, timedelta

from transit.ttc.realtime import TORONTO

DEPARTURE_WINDOW = timedelta(hours=3)
DEPARTURES_SHOWN = 12
# Each train's timetable track ahead, for smooth movement on the map.
TRACK_SECONDS = 120
TRACK_STEP = 10
M_PER_DEG_LAT = 111_130
M_PER_DEG_LON = 111_320 * math.cos(math.radians(43.7))

_lock = threading.Lock()
_index: dict = {}


def current_version(conn, today: date) -> str | None:
    """The newest loaded schedule covering today, else the newest one."""
    row = conn.execute(
        """SELECT feed_version FROM normalized.regional_versions
           ORDER BY (%s BETWEEN service_start AND service_end) DESC, loaded_at DESC LIMIT 1""",
        (today,),
    ).fetchone()
    return row["feed_version"] if row else None


def _xy(lon: float, lat: float) -> tuple[float, float]:
    return lon * M_PER_DEG_LON, lat * M_PER_DEG_LAT


def _project(path: list[tuple[float, float]], cum: list[float], x: float, y: float, start: int):
    """Distance along the path of the point nearest (x, y), searching from segment `start`
    so stations stay in order on lines that double back."""
    best = (math.inf, cum[start], start)
    for i in range(start, len(path) - 1):
        (ax, ay), (bx, by) = path[i], path[i + 1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        f = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        px, py = ax + f * dx, ay + f * dy
        d2 = (x - px) ** 2 + (y - py) ** 2
        if d2 < best[0]:
            best = (d2, cum[i] + f * math.sqrt(length2), i)
    return best[1], best[2]


def index(conn, version: str) -> dict:
    """Everything needed to place trains, built once per schedule version."""
    with _lock:
        if _index.get("version") == version:
            return _index
        lines = {
            r["line_id"]: dict(r)
            for r in conn.execute(
                """SELECT line_id, code, name, colour, text_colour FROM normalized.regional_lines
                   WHERE feed_version = %s ORDER BY code""",
                (version,),
            )
        }
        stations = {
            r["station_id"]: dict(r)
            for r in conn.execute(
                """SELECT station_id, name, lat, lon FROM normalized.regional_stations
                   WHERE feed_version = %s ORDER BY name""",
                (version,),
            )
        }
        shapes = {}
        for r in conn.execute(
            "SELECT shape_id, path FROM normalized.regional_shapes WHERE feed_version = %s",
            (version,),
        ):
            xy = [_xy(lon, lat) for lon, lat in r["path"]]
            cum = [0.0]
            for (ax, ay), (bx, by) in zip(xy, xy[1:], strict=False):
                cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
            shapes[r["shape_id"]] = {"lonlat": r["path"], "xy": xy, "cum": cum}
        services: dict[date, set] = {}
        for r in conn.execute(
            """SELECT service_id, service_date FROM normalized.regional_service_dates
               WHERE feed_version = %s""",
            (version,),
        ):
            services.setdefault(r["service_date"], set()).add(r["service_id"])
        trips = {
            r["trip_id"]: {**r, "stops": []}
            for r in conn.execute(
                """SELECT trip_id, line_id, service_id, destination, shape_id
                   FROM normalized.regional_trips WHERE feed_version = %s""",
                (version,),
            )
        }
        for r in conn.execute(
            """SELECT trip_id, station_id, arrival_s, departure_s
               FROM normalized.regional_stop_times WHERE feed_version = %s
               ORDER BY trip_id, stop_sequence""",
            (version,),
        ):
            trips[r["trip_id"]]["stops"].append((r["station_id"], r["arrival_s"], r["departure_s"]))
        by_service: dict[str, list] = {}
        projected: dict = {}
        for trip in trips.values():
            by_service.setdefault(trip["service_id"], []).append(trip)
            shape = shapes.get(trip["shape_id"])
            key = (trip["shape_id"], tuple(s[0] for s in trip["stops"]))
            if shape and key not in projected:
                along, segment = [], 0
                for station_id, _, _ in trip["stops"]:
                    s = stations[station_id]
                    x, y = _xy(s["lon"], s["lat"])
                    d, segment = _project(shape["xy"], shape["cum"], x, y, segment)
                    along.append(d)
                projected[key] = along
            trip["along"] = projected.get(key)
        _index.clear()
        _index.update(
            version=version,
            lines=lines,
            stations=stations,
            shapes=shapes,
            services=services,
            by_service=by_service,
        )
        return _index


def network(idx: dict) -> dict:
    """Lines with their track geometry, and stations with the lines serving them."""
    serving: dict[str, set] = {}
    paths: dict[str, dict] = {}
    for trips in idx["by_service"].values():
        for trip in trips:
            code = idx["lines"][trip["line_id"]]["code"]
            for station_id, _, _ in trip["stops"]:
                serving.setdefault(station_id, set()).add(code)
            if trip["shape_id"]:
                # Each direction has its own shape over the same track; draw one of them.
                ends = frozenset(s[0] for s in (trip["stops"][0], trip["stops"][-1]))
                paths.setdefault(trip["line_id"], {}).setdefault(ends, trip["shape_id"])
    return {
        "lines": [
            {
                "id": line_id,
                "code": line["code"],
                "name": line["name"],
                "colour": f"#{line['colour']}" if line["colour"] else None,
                "paths": [
                    [[round(lon, 5), round(lat, 5)] for lon, lat in idx["shapes"][s]["lonlat"]]
                    for s in sorted(set(paths.get(line_id, {}).values()))
                ],
            }
            for line_id, line in idx["lines"].items()
        ],
        "stations": [
            {
                "id": station_id,
                "name": s["name"],
                "lat": round(s["lat"], 5),
                "lon": round(s["lon"], 5),
                "lines": sorted(serving.get(station_id, ())),
            }
            for station_id, s in idx["stations"].items()
        ],
    }


def _day_start(day: date) -> datetime:
    """GTFS times count from noon minus 12 hours, which differs from midnight on the days
    clocks change."""
    return datetime.combine(day, time(12), TORONTO) - timedelta(hours=12)


def _service_days(idx: dict, now: datetime):
    """(service day start, trips) for yesterday and today: late trains run past midnight."""
    today = now.astimezone(TORONTO).date()
    for day in (today - timedelta(days=1), today):
        start = _day_start(day)
        for service_id in idx["services"].get(day, ()):
            yield start, idx["by_service"].get(service_id, [])


def _point(idx, trip, i: int, f: float):
    """Position and bearing a fraction f of the way from stop i to stop i + 1."""
    stops, stations = trip["stops"], idx["stations"]
    shape = idx["shapes"].get(trip["shape_id"])
    if shape and trip["along"]:
        a, b = trip["along"][i], trip["along"][min(i + 1, len(stops) - 1)]
        d = a + f * (b - a)
        cum, lonlat = shape["cum"], shape["lonlat"]
        k = max(0, min(len(cum) - 2, bisect_right(cum, d) - 1))
        seg = cum[k + 1] - cum[k]
        g = 0.0 if seg == 0 else (d - cum[k]) / seg
        (lon0, lat0), (lon1, lat1) = lonlat[k], lonlat[k + 1]
        lon, lat = lon0 + g * (lon1 - lon0), lat0 + g * (lat1 - lat0)
        if b < a:  # Travelling against the shape's direction.
            lon0, lat0, lon1, lat1 = lon1, lat1, lon0, lat0
    else:
        s0 = stations[stops[i][0]]
        s1 = stations[stops[min(i + 1, len(stops) - 1)][0]]
        lon0, lat0, lon1, lat1 = s0["lon"], s0["lat"], s1["lon"], s1["lat"]
        lon, lat = lon0 + f * (lon1 - lon0), lat0 + f * (lat1 - lat0)
    dx, dy = (lon1 - lon0) * M_PER_DEG_LON, (lat1 - lat0) * M_PER_DEG_LAT
    bearing = round(math.degrees(math.atan2(dx, dy)) % 360) if dx or dy else None
    return round(lat, 5), round(lon, 5), bearing


def _place(idx: dict, trip: dict, t: float):
    """(lat, lon, bearing, station it is at or None, index of the next stop) at `t`
    seconds into the service day, or None outside the trip."""
    stops = trip["stops"]
    if not stops[0][2] <= t <= stops[-1][1]:
        return None
    for i, (_, arrival, departure) in enumerate(stops):
        if arrival <= t <= departure:
            return (*_point(idx, trip, i, 0.0), stops[i][0], i + 1)
        if i + 1 < len(stops) and departure < t < stops[i + 1][1]:
            f = (t - departure) / (stops[i + 1][1] - departure)
            return (*_point(idx, trip, i, f), None, i + 1)
    return None


def trains(idx: dict, now: datetime) -> dict:
    """Every train that should be running now, placed by its timetable, with its `track`:
    [lon, lat, epoch seconds] every TRACK_STEP seconds for the next TRACK_SECONDS, so the
    map can move it smoothly between refreshes."""
    out = []
    for start, trips in _service_days(idx, now):
        t = (now - start).total_seconds()
        for trip in trips:
            place = _place(idx, trip, t)
            if place is None:
                continue
            lat, lon, bearing, at, upcoming = place
            stops = trip["stops"]
            track = []
            for step in range(0, TRACK_SECONDS + 1, TRACK_STEP):
                ahead = _place(idx, trip, t + step)
                if ahead is None:
                    break
                track.append([ahead[1], ahead[0], round(now.timestamp()) + step])
            line = idx["lines"][trip["line_id"]]
            nxt = stops[upcoming] if upcoming < len(stops) else None
            out.append(
                {
                    "id": trip["trip_id"],
                    "line": line["code"],
                    "line_name": line["name"],
                    "colour": f"#{line['colour']}" if line["colour"] else None,
                    "destination": trip["destination"],
                    "lat": lat,
                    "lon": lon,
                    "bearing": bearing,
                    "at_station": idx["stations"][at]["name"] if at else None,
                    "next_station": idx["stations"][nxt[0]]["name"] if nxt else None,
                    "next_arrival": (start + timedelta(seconds=nxt[1])).isoformat()
                    if nxt
                    else None,
                    "track": track,
                }
            )
    out.sort(key=lambda r: (r["line"], r["id"]))
    return {"source": "timetable", "trains": out}


def station(idx: dict, station_id: str, now: datetime) -> dict | None:
    """A station's lines and its next scheduled departures."""
    s = idx["stations"].get(station_id)
    if s is None:
        return None
    lines, departures = set(), []
    for start, trips in _service_days(idx, now):
        for trip in trips:
            stops = trip["stops"]
            for i, (sid, _, departure) in enumerate(stops):
                if sid != station_id:
                    continue
                line = idx["lines"][trip["line_id"]]
                lines.add((line["code"], line["name"], line["colour"]))
                at = start + timedelta(seconds=departure)
                # The last stop is an arrival: nothing departs from there on this trip.
                if i < len(stops) - 1 and now <= at <= now + DEPARTURE_WINDOW:
                    departures.append(
                        {
                            "line": line["code"],
                            "colour": f"#{line['colour']}" if line["colour"] else None,
                            "destination": trip["destination"],
                            "departs": at.isoformat(),
                            "minutes": int((at - now).total_seconds() // 60),
                            "trip_id": trip["trip_id"],
                        }
                    )
    departures.sort(key=lambda d: d["departs"])
    return {
        "source": "timetable",
        "station": {
            "id": station_id,
            "name": s["name"],
            "lat": s["lat"],
            "lon": s["lon"],
            "lines": [
                {"code": c, "name": n, "colour": f"#{col}" if col else None}
                for c, n, col in sorted(lines)
            ],
        },
        "departures": departures[:DEPARTURES_SHOWN],
    }
