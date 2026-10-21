"""Read queries for regional trains: lines, stations, train positions and departures.

Positions follow each trip's timetable along its track. When Metrolinx's live feeds are
collected (transit.regional_live), a train with a recent GPS report is placed where the
timetable has it at the point nearest that report, and then moves on at the timetable's
pace; one with only a delay is placed where the timetable had it that many seconds ago.
Each train and departure says whether it is live; without live data everything follows
the timetable, and nothing claims a train is late or on time.
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
# Live reports older than this are ignored, and GPS fixes further than this from the
# trip's track (a train on another line's track, or a bad fix).
LIVE_FRESH = timedelta(minutes=3)
GPS_OFF_TRACK_M = 1000
NO_LIVE = {"trains": {}, "stops": {}, "alerts": []}

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


def live(conn, now: datetime) -> dict:
    """Recent live state: trains by trip ID, predicted departures by trip and station,
    and the alerts in effect now."""
    since = now - LIVE_FRESH
    trains = {
        r["trip_id"]: dict(r)
        for r in conn.execute(
            """SELECT trip_id, lat, lon, reported_at, delay_seconds, delay_at, cancelled
               FROM normalized.regional_live_trains
               WHERE reported_at > %s OR delay_at > %s""",
            (since, since),
        )
    }
    stops: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT trip_id, station_id, predicted_departure, skipped
           FROM normalized.regional_live_stops"""
    ):
        if r["trip_id"] in trains:
            stops.setdefault(r["trip_id"], {})[r["station_id"]] = (
                r["predicted_departure"], r["skipped"]
            )  # fmt: skip
    alerts = [
        dict(r)
        for r in conn.execute(
            """SELECT alert_id, effect, header, description, url, line_ids, station_ids
               FROM normalized.regional_alerts
               WHERE (active_from IS NULL OR active_from <= %s)
                 AND (active_until IS NULL OR active_until > %s)
               ORDER BY header, alert_id""",
            (now, now),
        )
    ]
    return {"trains": trains, "stops": stops, "alerts": alerts}


def _nearest(xy, cum, x: float, y: float) -> tuple[float, float]:
    """(distance along the path, distance from it) of the path point nearest (x, y)."""
    best = (math.inf, 0.0)
    for i in range(len(xy) - 1):
        (ax, ay), (bx, by) = xy[i], xy[i + 1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        f = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        d2 = (x - ax - f * dx) ** 2 + (y - ay - f * dy) ** 2
        if d2 < best[0]:
            best = (d2, cum[i] + f * math.sqrt(length2))
    return best[1], math.sqrt(best[0])


def _time_at(idx: dict, trip: dict, lon: float, lat: float) -> float | None:
    """Seconds into the service day at which the timetable has the trip at the point of
    its track nearest (lon, lat); None if that point is over GPS_OFF_TRACK_M away."""
    stops = trip["stops"]
    shape = idx["shapes"].get(trip["shape_id"])
    if shape and trip["along"]:
        xy, cum, along = shape["xy"], shape["cum"], trip["along"]
    else:
        stations = idx["stations"]
        xy = [_xy(stations[s[0]]["lon"], stations[s[0]]["lat"]) for s in stops]
        cum = [0.0]
        for (ax, ay), (bx, by) in zip(xy, xy[1:], strict=False):
            cum.append(cum[-1] + math.hypot(bx - ax, by - ay))
        along = cum
    if len(xy) < 2:
        return None
    d, off = _nearest(xy, cum, *_xy(lon, lat))
    if off > GPS_OFF_TRACK_M:
        return None
    if along[-1] < along[0]:  # Travelling against the shape's direction.
        d, along = -d, [-a for a in along]
    if d <= along[0]:
        return float(stops[0][2])
    for i in range(len(stops) - 1):
        a, b = along[i], along[i + 1]
        if d <= b:
            departure, arrival = stops[i][2], stops[i + 1][1]
            f = 0.0 if b <= a else (d - a) / (b - a)
            return departure + f * (arrival - departure)
    return float(stops[-1][1])


def _timetable_time(idx: dict, trip: dict, t: float, info: dict | None, now: datetime):
    """(seconds into the service day to place the trip at, live delay in seconds or None,
    whether it is live). A recent GPS fix on the track wins; then the trip update's delay;
    else the timetable."""
    if not info:
        return t, None, False
    fresh = now - LIVE_FRESH
    delay = info["delay_seconds"] if info["delay_at"] and info["delay_at"] > fresh else None
    if info["reported_at"] and info["reported_at"] > fresh and info["lat"] is not None:
        at = _time_at(idx, trip, info["lon"], info["lat"])
        if at is not None:
            placed = at + (now - info["reported_at"]).total_seconds()
            return placed, delay if delay is not None else round(t - placed), True
    if delay is not None:
        return t - delay, delay, True
    return t, None, False


def _line_codes(idx: dict, line_ids) -> list[str]:
    """Line codes for live line IDs. The live feed's IDs carry the schedule's version
    prefix ('09261126-LW'), so an ID from a newer schedule is matched by its suffix."""
    by_suffix = {i.rsplit("-", 1)[-1]: line["code"] for i, line in idx["lines"].items()}
    codes = set()
    for line_id in line_ids:
        line = idx["lines"].get(line_id)
        code = line["code"] if line else by_suffix.get(line_id.rsplit("-", 1)[-1])
        if code:
            codes.add(code)
    return sorted(codes)


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


def trains(idx: dict, now: datetime, live_state: dict | None = None) -> dict:
    """Every train running now, placed by live data where there is some (_timetable_time)
    and by its timetable otherwise, with its `track`: [lon, lat, epoch seconds] every
    TRACK_STEP seconds for the next TRACK_SECONDS, so the map can move it smoothly between
    refreshes. Cancelled trains are left out. Line alerts in effect are listed too."""
    live_state = live_state or NO_LIVE
    out = []
    for start, trips in _service_days(idx, now):
        for trip in trips:
            info = live_state["trains"].get(trip["trip_id"])
            if info and info["cancelled"]:
                continue
            scheduled = (now - start).total_seconds()
            t, delay, is_live = _timetable_time(idx, trip, scheduled, info, now)
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
            next_arrival = None
            if nxt:
                predicted = live_state["stops"].get(trip["trip_id"], {}).get(nxt[0])
                next_arrival = (
                    predicted[0]
                    if predicted and predicted[0]
                    else start + timedelta(seconds=nxt[1] + (delay or 0))
                )
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
                    "next_arrival": next_arrival.isoformat() if next_arrival else None,
                    "live": is_live,
                    "delay_minutes": round(delay / 60) if delay is not None else None,
                    "track": track,
                }
            )
    out.sort(key=lambda r: (r["line"], r["id"]))
    alerts = [
        {
            "id": a["alert_id"],
            "header": a["header"],
            "description": a["description"],
            "lines": _line_codes(idx, a["line_ids"]),
        }
        for a in live_state["alerts"]
        if a["line_ids"]
    ]
    return {
        "source": "live" if any(r["live"] for r in out) else "timetable",
        "trains": out,
        "alerts": alerts,
    }


def _expected(live_state: dict, trip_id: str, station_id: str, scheduled: datetime, now):
    """(expected departure, delay in minutes, live, cancelled) at a station: the live
    prediction for that station, else the train's current delay, else the timetable."""
    info = live_state["trains"].get(trip_id)
    if not info:
        return scheduled, None, False, False
    predicted = live_state["stops"].get(trip_id, {}).get(station_id)
    if info["cancelled"] or (predicted and predicted[1]):
        return scheduled, None, True, True
    if predicted and predicted[0]:
        at = predicted[0]
    elif info["delay_seconds"] is not None and info["delay_at"] > now - LIVE_FRESH:
        at = scheduled + timedelta(seconds=info["delay_seconds"])
    else:
        return scheduled, None, False, False
    return at, round((at - scheduled).total_seconds() / 60), True, False


def station(
    idx: dict, station_id: str, now: datetime, live_state: dict | None = None
) -> dict | None:
    """A station's lines, its next departures (live where Metrolinx reports the train,
    with cancelled ones marked) and the alerts about it or its lines."""
    live_state = live_state or NO_LIVE
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
                # The last stop is an arrival: nothing departs from there on this trip.
                if i == len(stops) - 1:
                    continue
                scheduled = start + timedelta(seconds=departure)
                at, delay, is_live, cancelled = _expected(
                    live_state, trip["trip_id"], station_id, scheduled, now
                )
                if now <= at <= now + DEPARTURE_WINDOW:
                    departures.append(
                        {
                            "line": line["code"],
                            "colour": f"#{line['colour']}" if line["colour"] else None,
                            "destination": trip["destination"],
                            "departs": at.isoformat(),
                            "scheduled": scheduled.isoformat(),
                            "minutes": int((at - now).total_seconds() // 60),
                            "delay_minutes": delay,
                            "live": is_live,
                            "cancelled": cancelled,
                            "trip_id": trip["trip_id"],
                        }
                    )
    departures.sort(key=lambda d: d["departs"])
    codes = {code for code, _, _ in lines}
    alerts = [
        {"id": a["alert_id"], "header": a["header"], "description": a["description"]}
        for a in live_state["alerts"]
        if station_id in a["station_ids"] or codes & set(_line_codes(idx, a["line_ids"]))
    ]
    return {
        "source": "live" if any(d["live"] for d in departures) else "timetable",
        "alerts": alerts,
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
