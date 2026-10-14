"""Read-only queries behind the public API. Every function takes a connection and the
current time, so results are deterministic in tests."""

import math
import re
from datetime import datetime, timedelta

from transit.config import FUTURE_TOLERANCE_SECONDS, STALE_SECONDS
from transit.ttc.realtime import TORONTO, service_date

PREDICTIONS_STALE = timedelta(minutes=2)
ALERTS_STALE = timedelta(minutes=3)
BIKESHARE_STALE = timedelta(minutes=30)
AT_STATION_SECONDS = 20
STATUS_LABELS = {
    "no_service": "No service",
    "reduced_service": "Reduced service",
    "delays": "Delays",
    "modified_service": "Modified service",
}
STATUS_ORDER = ["no_service", "reduced_service", "delays", "modified_service"]


class NotFound(LookupError):
    pass


def feed_version(conn) -> str:
    row = conn.execute(
        """SELECT feed_version FROM normalized.ttc_gtfs_versions
           ORDER BY loaded_at DESC, feed_version LIMIT 1"""
    ).fetchone()
    if not row:
        raise NotFound("No TTC schedule loaded yet")
    return row["feed_version"]


def feed_state(conn) -> dict:
    return {
        r["feed"]: r
        for r in conn.execute("SELECT feed, snapshot_id, feed_timestamp FROM ops.ttc_feed_state")
    }


def freshness(state: dict, feed: str, now: datetime, limit: timedelta) -> dict:
    row = state.get(feed)
    as_of = row["feed_timestamp"] if row else None
    return {"as_of": as_of, "stale": as_of is None or now - as_of > limit}


def display_name(long_name: str, short_name: str) -> str:
    """'Line 2 (Bloor - Danforth)' -> 'Line 2 Bloor–Danforth'."""
    match = re.fullmatch(r"Line\s*\w+\s*\((.+)\)", long_name.strip())
    if not match:
        return long_name.strip()
    return f"Line {short_name} " + re.sub(r"\s*-\s*", "–", match.group(1).strip())


def lines(conn, version: str) -> list[dict]:
    rows = conn.execute(
        """SELECT r.route_id, r.short_name, r.long_name, r.color, r.text_color,
                  ls.direction_id, min(ls.towards) AS towards, count(*) AS stations
           FROM normalized.ttc_routes r
           JOIN normalized.ttc_line_stops ls USING (feed_version, route_id)
           WHERE r.feed_version = %s
           GROUP BY 1, 2, 3, 4, 5, 6 ORDER BY r.route_id::int, ls.direction_id""",
        (version,),
    ).fetchall()
    result = {}
    for r in rows:
        line = result.setdefault(
            r["route_id"],
            {
                "id": r["route_id"],
                "name": display_name(r["long_name"], r["short_name"]),
                "short_name": r["short_name"],
                "color": f"#{r['color']}" if r["color"] else None,
                "text_color": f"#{r['text_color']}" if r["text_color"] else None,
                "directions": [],
            },
        )
        line["directions"].append({"direction_id": r["direction_id"], "towards": r["towards"]})
    return list(result.values())


def line_stations(conn, version: str, route_id: str) -> list[dict]:
    """Stations in direction-0 order, each with its platform in each direction."""
    rows = conn.execute(
        """SELECT ls.direction_id, ls.stop_order, ls.stop_id, s.station_key, s.station_name
           FROM normalized.ttc_line_stops ls
           JOIN normalized.ttc_stops s USING (feed_version, stop_id)
           WHERE ls.feed_version = %s AND ls.route_id = %s
           ORDER BY ls.direction_id, ls.stop_order""",
        (version, route_id),
    ).fetchall()
    if not rows:
        raise NotFound(f"Unknown line {route_id}")
    stations = {}
    for r in rows:
        if r["direction_id"] == 0:
            stations[r["station_key"]] = {
                "key": r["station_key"],
                "name": r["station_name"],
                "platforms": {},
            }
    for r in rows:
        station = stations.get(r["station_key"])
        if station is not None:
            station["platforms"][r["direction_id"]] = {
                "stop_id": r["stop_id"],
                "stop_order": r["stop_order"],
            }
    return list(stations.values())


def segment_seconds(conn, version: str) -> dict[tuple, float]:
    """Median scheduled run time into each platform from the previous one on its line."""
    rows = conn.execute(
        """WITH st AS (
             SELECT ls.route_id, ls.direction_id, ls.stop_order, st.arrival_seconds,
                    lag(ls.stop_order) OVER w AS prev_order,
                    lag(st.departure_seconds) OVER w AS prev_departure
             FROM normalized.ttc_stop_times st
             JOIN normalized.ttc_line_stops ls USING (feed_version, stop_id)
             WHERE st.feed_version = %s
             WINDOW w AS (PARTITION BY st.trip_id ORDER BY st.stop_sequence))
           SELECT route_id, direction_id, stop_order,
                  percentile_cont(0.5) WITHIN GROUP
                    (ORDER BY arrival_seconds - prev_departure) AS seconds
           FROM st WHERE prev_order = stop_order - 1 GROUP BY 1, 2, 3""",
        (version,),
    ).fetchall()
    return {(r["route_id"], r["direction_id"], r["stop_order"]): r["seconds"] for r in rows}


def scheduled_headways(conn, version: str, route_id: str, now: datetime) -> dict[int, float]:
    """Scheduled seconds between trains per direction, from trips within +/-30 minutes at
    a mid-line platform on today's active service."""
    day = service_date(now)
    local_midnight = datetime.combine(day, datetime.min.time(), TORONTO)
    seconds_now = int((now - local_midnight).total_seconds())
    weekday = day.strftime("%A").lower()
    rows = conn.execute(
        f"""WITH services AS (
              (SELECT service_id FROM normalized.ttc_calendar
               WHERE feed_version = %(v)s AND {weekday}
                 AND start_date <= %(d)s AND end_date >= %(d)s
               UNION
               SELECT service_id FROM normalized.ttc_calendar_dates
               WHERE feed_version = %(v)s AND date = %(d)s AND exception_type = 1)
              EXCEPT
              SELECT service_id FROM normalized.ttc_calendar_dates
              WHERE feed_version = %(v)s AND date = %(d)s AND exception_type = 2),
            mid AS (
              SELECT direction_id, stop_id FROM normalized.ttc_line_stops ls
              WHERE feed_version = %(v)s AND route_id = %(r)s
                AND stop_order = (SELECT (max(stop_order) + 1) / 2
                                  FROM normalized.ttc_line_stops x
                                  WHERE x.feed_version = ls.feed_version
                                    AND x.route_id = ls.route_id
                                    AND x.direction_id = ls.direction_id))
            SELECT mid.direction_id, count(*) AS trips
            FROM mid
            JOIN normalized.ttc_stop_times st
              ON st.feed_version = %(v)s AND st.stop_id = mid.stop_id
             AND st.arrival_seconds BETWEEN %(lo)s AND %(hi)s
            JOIN normalized.ttc_trips t
              ON t.feed_version = %(v)s AND t.trip_id = st.trip_id
            WHERE t.service_id IN (SELECT service_id FROM services)
            GROUP BY 1""",
        {
            "v": version,
            "d": day,
            "r": route_id,
            "lo": seconds_now - 1800,
            "hi": seconds_now + 1800,
        },
    ).fetchall()
    return {r["direction_id"]: 3600 / r["trips"] for r in rows if r["trips"]}


def train_positions(conn, route_id: str, stations: list[dict], segments: dict, now: datetime):
    """Estimate where each train is from its predicted arrivals (the subway feed has no
    vehicle positions).

    TTC often predicts the first listed platform at the current time even while the train
    is between stations, so the next platform is the first one predicted more than
    AT_STATION_SECONDS ahead. If that arrival is further away than the scheduled run from
    the previous platform, the train has not left the previous platform yet.
    """
    index = {}
    for i, station in enumerate(stations):
        for direction, platform in station["platforms"].items():
            index[(direction, platform["stop_order"])] = i
    by_train = {}
    for r in conn.execute(
        """SELECT train_id, direction_id, stop_order, predicted_arrival
           FROM normalized.ttc_current_predictions
           WHERE route_id = %s AND predicted_arrival >= %s
           ORDER BY train_id, stop_order""",
        (route_id, now - timedelta(seconds=AT_STATION_SECONDS)),
    ):
        by_train.setdefault(r["train_id"], []).append(r)
    cumulative = {}
    for (route, direction, order), seconds in sorted(segments.items()):
        if route == route_id:
            previous = cumulative.get((direction, order - 1), 0.0)
            cumulative[(direction, order)] = previous + seconds
    trains = []
    for train_id, predictions in by_train.items():
        upcoming = [
            p
            for p in predictions
            if (p["predicted_arrival"] - now).total_seconds() > AT_STATION_SECONDS
        ]
        target = upcoming[0] if upcoming else predictions[-1]
        direction, order = target["direction_id"], target["stop_order"]
        next_index = index.get((direction, order))
        if next_index is None:
            continue
        eta = max(0.0, (target["predicted_arrival"] - now).total_seconds())
        segment = segments.get((route_id, direction, order))
        previous_index = index.get((direction, order - 1))
        if not upcoming or previous_index is None or not segment:
            # No later platform listed, or waiting at the first platform of the line.
            fraction, position, at_index = 0.0, float(next_index), next_index
        elif eta >= segment:
            fraction, position, at_index = 1.0, float(previous_index), previous_index
        else:
            fraction = eta / segment
            position = next_index + (previous_index - next_index) * fraction
            at_index = None
        trains.append(
            {
                "train_id": train_id,
                "direction_id": direction,
                # For moving the train between refreshes (static/motion.js): it runs from
                # from_index to next_index over segment_seconds, arriving at arrives_at.
                "from_index": previous_index if upcoming and segment else None,
                "next_index": next_index,
                "arrives_at": target["predicted_arrival"].isoformat(),
                "segment_seconds": round(segment) if segment else None,
                "at_station": at_index is not None,
                "station": stations[next_index if at_index is None else at_index]["name"],
                "next_station": stations[next_index]["name"],
                "eta_seconds": round(eta),
                "position": round(position, 3),
                "in_service": order > 1,
                "_progress": cumulative.get((direction, order), 0.0) - (segment or 0.0) * fraction,
            }
        )
    return trains


def gaps(trains: list[dict], headways: dict[int, float]) -> list[dict]:
    """Scheduled running time separating consecutive trains, flagged when far above the
    scheduled headway. Trains waiting at their first platform are excluded."""
    result = []
    for direction in (0, 1):
        moving = sorted(
            (t for t in trains if t["direction_id"] == direction and t["in_service"]),
            key=lambda t: t["_progress"],
            reverse=True,
        )
        headway = headways.get(direction)
        for ahead, behind in zip(moving, moving[1:], strict=False):
            seconds = ahead["_progress"] - behind["_progress"]
            long_gap = bool(headway and seconds > max(2 * headway, headway + 300))
            result.append(
                {
                    "direction_id": direction,
                    "from_position": behind["position"],
                    "to_position": ahead["position"],
                    "seconds": round(seconds),
                    "scheduled_headway_seconds": round(headway) if headway else None,
                    "long": long_gap,
                }
            )
    return result


def current_alerts(conn, now: datetime) -> list[dict]:
    """Current version of every alert in each alert feed's newest snapshot."""
    rows = conn.execute(
        """SELECT v.feed, v.alert_id, v.cause, v.effect, v.header_text, v.description_text,
                  v.url, v.active_periods, v.route_ids, v.stop_ids, v.derived_status,
                  v.status_mismatch, v.advance_notice, a.first_seen_at
           FROM normalized.ttc_alerts a
           JOIN ops.ttc_feed_state s ON s.feed = a.feed AND s.snapshot_id = a.last_snapshot_id
           JOIN normalized.ttc_alert_versions v
             ON v.feed = a.feed AND v.alert_id = a.alert_id
            AND v.version_hash = a.current_version_hash
           ORDER BY a.first_seen_at DESC, v.alert_id"""
    ).fetchall()
    epoch = now.timestamp()
    alerts = []
    for r in rows:
        periods = r["active_periods"] or [{"start": None, "end": None}]
        active = any(
            (p["start"] or 0) <= epoch and (p["end"] is None or epoch <= p["end"]) for p in periods
        )
        upcoming = not active and any((p["start"] or 0) > epoch for p in periods)
        if not active and not upcoming:
            continue
        if r["advance_notice"]:
            # A notice's active period is when TTC displays it, not when service stops.
            active, upcoming = False, True
        alerts.append(
            {
                "id": r["alert_id"],
                "kind": "accessibility" if r["feed"] == "alerts_accessibility" else "service",
                "timing": "active" if active else "upcoming",
                "status": r["derived_status"],
                "effect": r["effect"],
                "cause": r["cause"],
                "header": r["header_text"],
                "description": r["description_text"],
                "url": r["url"],
                "periods": [
                    {
                        "start": _iso(p["start"]),
                        "end": _iso(p["end"]),
                    }
                    for p in r["active_periods"]
                ],
                "lines": r["route_ids"],
                "stop_ids": r["stop_ids"],
                "effect_contradicts_text": r["status_mismatch"],
                "advance_notice": r["advance_notice"],
                "first_seen": r["first_seen_at"],
            }
        )
    return alerts


def _iso(epoch):
    return datetime.fromtimestamp(epoch, TORONTO).isoformat() if epoch else None


def line_statuses(
    line_list: list[dict], alerts: list[dict], alerts_fresh: dict, detected: dict | None = None
) -> list[dict]:
    """Reported status per line (the most severe active TTC service alert) and, only when
    TTC reports nothing, our detected possible delay."""
    statuses = []
    for line in line_list:
        active = [
            a
            for a in alerts
            if a["kind"] == "service" and a["timing"] == "active" and line["id"] in a["lines"]
        ]
        ranked = [a for a in active if a["status"] in STATUS_ORDER]
        ranked.sort(key=lambda a: STATUS_ORDER.index(a["status"]))
        if alerts_fresh["stale"]:
            status, label = "unknown", "Status unavailable"
        elif ranked:
            status, label = ranked[0]["status"], STATUS_LABELS[ranked[0]["status"]]
        else:
            status, label = "normal", "Normal service"
        statuses.append(
            {
                "line": line["id"],
                "name": line["name"],
                "status": status,
                "label": label,
                "source": "reported",
                "alerts": [a["id"] for a in active],
                "summary": ranked[0]["header"] if ranked else None,
                "detected": (
                    {
                        "status": "possible_delay",
                        "label": "Possible delay, not confirmed by TTC",
                        "source": "detected",
                        "incidents": (detected or {})[line["id"]],
                    }
                    if status == "normal" and (detected or {}).get(line["id"])
                    else None
                ),
            }
        )
    return statuses


def station(conn, version: str, key: str, now: datetime) -> dict:
    platforms = conn.execute(
        """SELECT s.stop_id, s.station_name, s.platform, s.lat, s.lon, s.wheelchair_boarding,
                  ls.route_id, ls.direction_id, ls.towards
           FROM normalized.ttc_stops s
           JOIN normalized.ttc_line_stops ls USING (feed_version, stop_id)
           WHERE s.feed_version = %s AND s.station_key = %s
           ORDER BY ls.route_id::int, ls.direction_id""",
        (version, key),
    ).fetchall()
    if not platforms:
        raise NotFound(f"Unknown station {key}")
    stop_ids = [p["stop_id"] for p in platforms]
    predictions = conn.execute(
        """SELECT stop_id, train_id, predicted_arrival FROM (
             SELECT stop_id, train_id, predicted_arrival,
                    row_number() OVER (PARTITION BY stop_id ORDER BY predicted_arrival) AS n
             FROM normalized.ttc_current_predictions
             WHERE stop_id = ANY(%s) AND predicted_arrival >= %s) p
           WHERE n <= 3 ORDER BY predicted_arrival""",
        (stop_ids, now - timedelta(seconds=AT_STATION_SECONDS)),
    ).fetchall()
    by_stop = {}
    for p in predictions:
        by_stop.setdefault(p["stop_id"], []).append(
            {
                "train_id": p["train_id"],
                "arrival": p["predicted_arrival"],
                "minutes": max(0, math.floor((p["predicted_arrival"] - now).total_seconds() / 60)),
            }
        )
    lat = sum(p["lat"] for p in platforms) / len(platforms)
    lon = sum(p["lon"] for p in platforms) / len(platforms)
    return {
        "key": key,
        "name": platforms[0]["station_name"],
        "lat": lat,
        "lon": lon,
        "lines": sorted({p["route_id"] for p in platforms}, key=int),
        "directions": [
            {
                "line": p["route_id"],
                "direction_id": p["direction_id"],
                "towards": p["towards"],
                "platform": p["platform"],
                "stop_id": p["stop_id"],
                "arrivals": by_stop.get(p["stop_id"], []),
            }
            for p in platforms
        ],
        "stop_ids": stop_ids,
    }


def nearest_bikes(conn, lat: float, lon: float, now: datetime, limit: int = 2) -> dict:
    """Nearest Bike Share docks from the latest successful Bike Share collection."""
    rows = conn.execute(
        """WITH latest AS (
             SELECT collection_id, collected_at FROM ops.ingestion_runs
             WHERE status = 'succeeded' ORDER BY collected_at DESC LIMIT 1)
           SELECT s.station_id, s.name, s.lat, s.lon, s.num_bikes_available,
                  s.num_docks_available, s.is_renting, s.station_reported_at, l.collected_at
           FROM latest l
           JOIN normalized.station_observations s USING (collection_id)
           WHERE s.observed
           ORDER BY (s.lat - %(lat)s) ^ 2 + ((s.lon - %(lon)s) * cos(radians(%(lat)s))) ^ 2
           LIMIT %(limit)s""",
        {"lat": lat, "lon": lon, "limit": limit},
    ).fetchall()
    collected_at = rows[0]["collected_at"] if rows else None
    return {
        "as_of": collected_at,
        "stale": collected_at is None or now - collected_at > BIKESHARE_STALE,
        "docks": [
            {
                "id": r["station_id"],
                "name": r["name"],
                "distance_m": round(_metres(lat, lon, r["lat"], r["lon"])),
                "bikes": r["num_bikes_available"] if r["is_renting"] else 0,
                "docks": r["num_docks_available"],
                "renting": r["is_renting"],
            }
            for r in rows
        ],
    }


def map_network(conn, version: str) -> dict:
    """Subway stations with coordinates, and each line as its stations in order."""
    rows = conn.execute(
        """SELECT ls.route_id, ls.stop_order, s.station_key, s.station_name,
                  avg(s.lat) OVER (PARTITION BY s.station_key) AS lat,
                  avg(s.lon) OVER (PARTITION BY s.station_key) AS lon
           FROM normalized.ttc_line_stops ls
           JOIN normalized.ttc_stops s USING (feed_version, stop_id)
           WHERE ls.feed_version = %s AND ls.direction_id = 0
           ORDER BY ls.route_id::int, ls.stop_order""",
        (version,),
    ).fetchall()
    paths, stations = {}, {}
    for r in rows:
        point = [round(r["lat"], 5), round(r["lon"], 5)]
        paths.setdefault(r["route_id"], []).append(point)
        station = stations.setdefault(
            r["station_key"],
            {
                "key": r["station_key"],
                "name": r["station_name"],
                "lat": point[0],
                "lon": point[1],
                "lines": [],
            },
        )
        station["lines"].append(r["route_id"])
    # Every platform of each station, to attach elevator and escalator alerts to it.
    for r in conn.execute(
        """SELECT station_key, array_agg(stop_id ORDER BY stop_id) AS stop_ids
           FROM normalized.ttc_stops WHERE feed_version = %s AND station_key IS NOT NULL
           GROUP BY station_key""",
        (version,),
    ):
        if r["station_key"] in stations:
            stations[r["station_key"]]["stop_ids"] = r["stop_ids"]
    return {"paths": paths, "stations": list(stations.values())}


def bike_docks(conn, now: datetime) -> dict:
    """Every dock in the latest successful Bike Share collection. As on the dashboard, a
    dock that is not installed or whose own report is stale has no current availability."""
    rows = conn.execute(
        """WITH latest AS (
             SELECT collection_id, collected_at FROM ops.ingestion_runs
             WHERE status = 'succeeded' ORDER BY collected_at DESC LIMIT 1)
           SELECT s.station_id, s.name, s.lat, s.lon, s.capacity, s.num_bikes_available,
                  s.num_docks_available, s.is_installed, s.is_renting, s.is_returning,
                  s.station_reported_at, l.collected_at
           FROM latest l
           JOIN normalized.station_observations s USING (collection_id)
           WHERE s.observed
           ORDER BY s.station_id""",
    ).fetchall()
    collected_at = rows[0]["collected_at"] if rows else None
    docks = []
    for r in rows:
        age = (now - r["station_reported_at"]).total_seconds()
        current = r["is_installed"] and -FUTURE_TOLERANCE_SECONDS <= age <= STALE_SECONDS
        docks.append(
            {
                "id": r["station_id"],
                "name": r["name"],
                "lat": round(r["lat"], 5),
                "lon": round(r["lon"], 5),
                "capacity": r["capacity"],
                "current": current,
                "bikes": (r["num_bikes_available"] if r["is_renting"] else 0) if current else None,
                "docks": (r["num_docks_available"] if r["is_returning"] else 0)
                if current
                else None,
                "reported_at": r["station_reported_at"],
            }
        )
    return {
        "as_of": collected_at,
        "stale": collected_at is None or now - collected_at > BIKESHARE_STALE,
        "docks": docks,
    }


def _metres(lat1, lon1, lat2, lon2):
    radius = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def all_stations(conn, version: str) -> list[dict]:
    return [
        {"key": r["station_key"], "name": r["station_name"], "lines": r["lines"]}
        for r in conn.execute(
            """SELECT s.station_key, min(s.station_name) AS station_name,
                      array_agg(DISTINCT ls.route_id ORDER BY ls.route_id) AS lines
               FROM normalized.ttc_stops s
               JOIN normalized.ttc_line_stops ls USING (feed_version, stop_id)
               WHERE s.feed_version = %s GROUP BY 1 ORDER BY 2""",
            (version,),
        )
    ]


DETECTION_WINDOW = timedelta(minutes=90)
COLLECTION_GAP = timedelta(minutes=2)
COMPASS = {"Northbound", "Southbound", "Eastbound", "Westbound"}


def platform_waits(conn, version: str, now: datetime) -> list[dict]:
    """Last observed arrival and next predicted arrival at every non-terminal platform."""
    return conn.execute(
        """WITH platforms AS (
             SELECT ls.route_id, ls.direction_id, ls.stop_order, ls.stop_id, ls.towards,
                    s.station_name, s.station_key, s.platform,
                    max(ls.stop_order) OVER (PARTITION BY ls.route_id, ls.direction_id)
                      AS last_order
             FROM normalized.ttc_line_stops ls
             JOIN normalized.ttc_stops s USING (feed_version, stop_id)
             WHERE ls.feed_version = %(v)s),
           last_arrival AS (
             SELECT stop_id, max(predicted_arrival) AS at
             FROM normalized.ttc_train_stop_events
             WHERE passed_at IS NOT NULL AND predicted_arrival > %(since)s
               AND predicted_arrival <= %(now)s
             GROUP BY stop_id),
           next_arrival AS (
             SELECT stop_id, min(predicted_arrival) AS at
             FROM normalized.ttc_current_predictions
             WHERE predicted_arrival >= %(now)s - interval '20 seconds'
             GROUP BY stop_id)
           SELECT p.*, la.at AS last_arrival, na.at AS next_arrival
           FROM platforms p
           LEFT JOIN last_arrival la USING (stop_id)
           LEFT JOIN next_arrival na USING (stop_id)
           WHERE p.stop_order > 1 AND p.stop_order < p.last_order
           ORDER BY p.route_id, p.direction_id, p.stop_order""",
        {"v": version, "since": now - DETECTION_WINDOW, "now": now},
    ).fetchall()


def collection_gaps(conn, now: datetime) -> list[tuple]:
    """Intervals in the detection window without a new trip-update snapshot."""
    rows = conn.execute(
        """SELECT DISTINCT feed_timestamp FROM ops.ttc_poll_runs
           WHERE feed = 'trips_subway' AND status = 'stored' AND feed_timestamp > %s
           ORDER BY feed_timestamp""",
        (now - DETECTION_WINDOW - COLLECTION_GAP,),
    ).fetchall()
    times = [now - DETECTION_WINDOW - COLLECTION_GAP, *(r["feed_timestamp"] for r in rows), now]
    return [(a, b) for a, b in zip(times, times[1:], strict=False) if b - a > COLLECTION_GAP]


def _incident(run: list[dict]) -> dict:
    worst = max(run, key=lambda r: r["waited"])
    where = (
        f"No {worst['platform'].lower()} train"
        if worst["platform"] in COMPASS
        else f"No train towards {worst['towards']}"
    )
    return {
        "direction_id": worst["direction_id"],
        "towards": worst["towards"],
        "station": worst["station_name"],
        "station_key": worst["station_key"],
        "platforms_affected": len(run),
        "waited_seconds": round(worst["waited"]),
        "scheduled_headway_seconds": round(worst["headway"]),
        "next_train_seconds": None if worst["next_in"] is None else round(worst["next_in"]),
        "message": f"{where} at {worst['station_name']} in {int(worst['waited'] // 60)} min",
    }


def detected_delays(
    waits: list[dict], headways: dict[str, dict[int, float]], gaps: list[tuple], now: datetime
) -> dict[str, list[dict]]:
    """Our own inference, separate from TTC alerts: at least two adjacent platforms in one
    direction have gone far longer than the scheduled headway without a train, with no
    train about to arrive and no collection gap that could explain the silence."""
    runs, run = [], []
    for w in waits:
        headway = headways.get(w["route_id"], {}).get(w["direction_id"])
        flagged = None
        if headway and w["last_arrival"] is not None:
            waited = (now - w["last_arrival"]).total_seconds()
            next_in = (w["next_arrival"] - now).total_seconds() if w["next_arrival"] else None
            blind = any(start < now and end > w["last_arrival"] for start, end in gaps)
            if (
                waited > max(2 * headway, headway + 300)
                and (next_in is None or next_in > 60)
                and not blind
            ):
                flagged = {**w, "waited": waited, "next_in": next_in, "headway": headway}
        adjacent = (
            flagged
            and run
            and (flagged["route_id"], flagged["direction_id"])
            == (run[-1]["route_id"], run[-1]["direction_id"])
            and flagged["stop_order"] == run[-1]["stop_order"] + 1
        )
        if not adjacent:
            runs.append(run)
            run = []
        if flagged:
            run.append(flagged)
    runs.append(run)
    incidents = {}
    for r in runs:
        if len(r) >= 2:
            incidents.setdefault(r[0]["route_id"], []).append(_incident(r))
    return incidents


def reliability(conn, now: datetime) -> dict:
    today = service_date(now)
    week_start = today - timedelta(days=6)
    longest = conn.execute(
        """SELECT route_id, gap_rank, direction_id, towards, station_key, station_name,
                  platform, gap_start, gap_end, gap_seconds, scheduled_headway_seconds
           FROM analytics.ttc_longest_gaps
           WHERE service_date = %s AND gap_rank <= 5 ORDER BY route_id::int, gap_rank""",
        (today,),
    ).fetchall()
    hourly = conn.execute(
        """SELECT route_id, service_hour,
                  sum(observed_headways) FILTER (WHERE service_date = %(today)s) AS today_n,
                  sum(regular_headways) FILTER (WHERE service_date = %(today)s)
                    AS today_regular,
                  sum(compared_headways) FILTER (WHERE service_date = %(today)s)
                    AS today_compared,
                  sum(long_gaps) FILTER (WHERE service_date = %(today)s) AS today_long,
                  sum(regular_headways) AS week_regular,
                  sum(compared_headways) AS week_compared,
                  sum(long_gaps) AS week_long,
                  round(avg(median_headway_seconds)) AS week_median,
                  round(avg(scheduled_headway_seconds)) AS week_scheduled
           FROM analytics.ttc_headway_reliability_hourly
           WHERE service_date BETWEEN %(start)s AND %(today)s
           GROUP BY 1, 2 ORDER BY route_id::int, service_hour""",
        {"today": today, "start": week_start},
    ).fetchall()
    outages = conn.execute(
        """SELECT station_key, station_name, device_type, header_text, first_seen_at,
                  resolved_at, active, duration_minutes, began_before_collection
           FROM analytics.ttc_elevator_outages
           WHERE active OR resolved_at >= %s
           ORDER BY active DESC, duration_minutes DESC""",
        (now - timedelta(days=7),),
    ).fetchall()
    coverage = conn.execute(
        """SELECT min(service_date) AS first_day, count(DISTINCT service_date) AS days,
                  count(*) AS headways
           FROM analytics.ttc_headways"""
    ).fetchone()
    built = conn.execute(
        "SELECT max(finished_at) AS at FROM ops.transformation_runs WHERE status = 'succeeded'"
    ).fetchone()["at"]

    def pct(part, whole):
        return round(100 * part / whole) if whole else None

    by_line = {}
    for r in hourly:
        by_line.setdefault(r["route_id"], []).append(
            {
                "hour": r["service_hour"],
                "today_regular_pct": pct(r["today_regular"], r["today_compared"]),
                "today_long_gaps": r["today_long"],
                "today_headways": r["today_n"],
                "week_regular_pct": pct(r["week_regular"], r["week_compared"]),
                "week_long_gaps": r["week_long"],
                "week_median_headway_seconds": r["week_median"],
                "week_scheduled_headway_seconds": r["week_scheduled"],
            }
        )
    resolved = sorted(o["duration_minutes"] for o in outages if not o["active"])
    return {
        "service_date": today,
        "analytics_built_at": built,
        "coverage": coverage,
        "longest_gaps_today": longest,
        "hourly": by_line,
        "outages": {
            "active": [o for o in outages if o["active"]],
            "resolved_last_7_days": len(resolved),
            "median_resolved_minutes": resolved[len(resolved) // 2] if resolved else None,
            "longest_resolved_minutes": resolved[-1] if resolved else None,
        },
    }
