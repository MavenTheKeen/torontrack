"""Live regional trains from Metrolinx's GTFS-Realtime API (docs/regional.md#live-trains).

The API needs a key (METROLINX_API_KEY_FILE); without one nothing is polled and the site
places trains by the timetable alone. The feeds cover GO buses too; only trains on the
loaded rail lines are kept. Each poll replaces the previous state: train positions and
delays, predicted departures at the stations ahead, and alerts about rail lines or
stations. The agency collector (transit.agencies.run_forever) runs these polls.

Metrolinx's terms allow the data to be shown but not passed on as a feed, and do not
allow its marks to be used with it, so alert text is stripped of them (`unmark`) and
none of this goes into the open-data exports.
"""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from transit.bikeshare.parsing import FeedValidationError
from transit.http import FeedClient, FetchError
from transit.ttc import realtime

HOST = "api.openmetrolinx.com"
FEEDS = {"vehicles": "VehiclePosition", "trips": "TripUpdates", "alerts": "Alerts"}
# The protobuf form is about a sixth of the JSON the API sends by default.
PROTOBUF = {"Accept": "application/x-protobuf"}
# Trains no longer listed in either feed for this long have finished and are removed.
KEEP_TRAINS = timedelta(minutes=10)
pb = realtime.gtfs_realtime_pb2


def feed_url(feed: str, key: str) -> str:
    return f"https://{HOST}/OpenDataAPI/api/V1/Gtfs/Feed/{FEEDS[feed]}?{urlencode({'key': key})}"


def unmark(text: str) -> str:
    """Alert text without Metrolinx's marks, as its licence requires: 'Old Cummer GO -
    Construction' -> 'Old Cummer - Construction', 'GO trains' -> 'Trains'. The feed also
    sends U+FFFD where its text had a dash."""
    text = text.replace("�", "–")
    text = re.sub(r"\bGO Transit\b", "Metrolinx", text)
    text = re.sub(r"\bUP Express\b", "airport train", text)
    text = re.sub(r"(?<=\S)\s+GO(\s+Station)?\b", "", text)
    text = re.sub(r"\bGO\s+(?=(?:trains?|bus(?:es)?|service)\b)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bGO\b", "regional", text)
    return text[:1].upper() + text[1:]


def rail(conn) -> tuple[set[str], set[str]]:
    """(line IDs, station IDs) of every loaded regional rail schedule."""
    lines = {
        r["line_id"] for r in conn.execute("SELECT DISTINCT line_id FROM normalized.regional_lines")
    }
    stations = {
        r["station_id"]
        for r in conn.execute("SELECT DISTINCT station_id FROM normalized.regional_stations")
    }
    return lines, stations


def parse_vehicles(snapshot: realtime.Snapshot, lines: set[str]) -> list[dict]:
    out = {}
    for entity in snapshot.message.entity:
        v = entity.vehicle
        if not entity.HasField("vehicle") or v.trip.route_id not in lines:
            continue
        if not v.trip.trip_id or not v.HasField("position"):
            continue
        lat, lon = v.position.latitude, v.position.longitude
        if not (42.5 < lat < 45.5 and -81.5 < lon < -78):
            continue
        out[v.trip.trip_id] = {
            "trip_id": v.trip.trip_id,
            "line_id": v.trip.route_id,
            "vehicle_id": v.vehicle.id or None,
            "lat": lat,
            "lon": lon,
            "reported_at": datetime.fromtimestamp(v.timestamp, UTC)
            if v.timestamp
            else snapshot.feed_timestamp,
        }
    return list(out.values())


def parse_trips(snapshot: realtime.Snapshot, lines: set[str]) -> tuple[list[dict], list[tuple]]:
    """(trains, stops): each train's delay at its next station and whether it is
    cancelled, and (trip, station, predicted departure, skipped) for the stations ahead."""
    trains, stops = {}, {}
    skipped = pb.TripUpdate.StopTimeUpdate.SKIPPED
    for entity in snapshot.message.entity:
        update = entity.trip_update
        if not entity.HasField("trip_update") or update.trip.route_id not in lines:
            continue
        trip_id = update.trip.trip_id
        if not trip_id:
            continue
        cancelled = update.trip.schedule_relationship in (pb.TripDescriptor.CANCELED, 7)
        delay = None
        for stu in update.stop_time_update:
            if not stu.stop_id:
                continue
            event = stu.departure if stu.HasField("departure") else stu.arrival
            if delay is None and stu.schedule_relationship != skipped:
                delay = event.delay
            at = datetime.fromtimestamp(event.time, UTC) if event.time else None
            stops[(trip_id, stu.stop_id)] = (
                trip_id,
                stu.stop_id,
                at,
                stu.schedule_relationship == skipped,
            )
        trains[trip_id] = {
            "trip_id": trip_id,
            "line_id": update.trip.route_id,
            "vehicle_id": update.vehicle.id or None,
            "delay_seconds": delay,
            "cancelled": cancelled,
            "delay_at": datetime.fromtimestamp(update.timestamp, UTC)
            if update.timestamp
            else snapshot.feed_timestamp,
        }
    return list(trains.values()), list(stops.values())


def parse_alerts(snapshot: realtime.Snapshot, lines: set[str], stations: set[str]) -> list[dict]:
    """Alerts naming a rail line or station, without the bus ones."""
    out = []
    for entity in snapshot.message.entity:
        alert = entity.alert
        if not entity.HasField("alert"):
            continue
        line_ids = sorted({i.route_id for i in alert.informed_entity if i.route_id in lines})
        station_ids = sorted({i.stop_id for i in alert.informed_entity if i.stop_id in stations})
        header = realtime._text(alert.header_text)
        if not (line_ids or station_ids) or not header:
            continue
        periods = list(alert.active_period)
        out.append(
            {
                "alert_id": entity.id,
                "effect": pb.Alert.Effect.Name(alert.effect),
                "header": unmark(header),
                "description": unmark(realtime._text(alert.description_text) or "") or None,
                "url": realtime._text(alert.url) or None,
                "line_ids": line_ids,
                "station_ids": station_ids,
                "active_from": datetime.fromtimestamp(periods[0].start, UTC)
                if periods and periods[0].start
                else None,
                "active_until": datetime.fromtimestamp(periods[-1].end, UTC)
                if periods and periods[-1].end
                else None,
            }
        )
    return out


def _fetch(client: FeedClient, feed: str, key: str) -> realtime.Snapshot:
    return realtime.decode(client.fetch_bytes(feed_url(feed, key), host=HOST, headers=PROTOBUF))


def _record(conn, feed: str, now: datetime, rows=None, feed_timestamp=None, error=None):
    conn.execute(
        """INSERT INTO ops.regional_polls (feed, polled_at, status, succeeded_at, rows,
               feed_timestamp, error)
           VALUES (%s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (feed) DO UPDATE SET polled_at = EXCLUDED.polled_at,
               status = EXCLUDED.status,
               succeeded_at = COALESCE(EXCLUDED.succeeded_at, regional_polls.succeeded_at),
               rows = EXCLUDED.rows,
               feed_timestamp = COALESCE(EXCLUDED.feed_timestamp, regional_polls.feed_timestamp),
               error = EXCLUDED.error""",
        (
            feed,
            now,
            "failed" if error else "ok",
            None if error else now,
            rows,
            feed_timestamp,
            error,
        ),  # fmt: skip
    )


def poll(conn, client: FeedClient, key: str, now: datetime, alerts: bool = False) -> dict:
    """Poll the vehicle and trip feeds, and the alerts too when `alerts`. A failed feed
    keeps its previous state, which the site ignores once it is stale."""
    lines, stations = rail(conn)
    result = {}
    for feed in ("vehicles", "trips", "alerts") if alerts else ("vehicles", "trips"):
        try:
            snapshot = _fetch(client, feed, key)
            if feed == "vehicles":
                count = _store_vehicles(conn, parse_vehicles(snapshot, lines), now)
            elif feed == "trips":
                count = _store_trips(conn, *parse_trips(snapshot, lines), now)
            else:
                count = _store_alerts(conn, parse_alerts(snapshot, lines, stations))
        except (FetchError, FeedValidationError) as exc:
            _record(conn, feed, now, error=str(exc)[:1000])
            result[feed] = {"status": "failed", "error": str(exc)[:200]}
            continue
        _record(conn, feed, now, count, snapshot.feed_timestamp)
        result[feed] = {"status": "ok", "rows": count}
    conn.execute(
        "DELETE FROM normalized.regional_live_trains WHERE updated_at < %s", (now - KEEP_TRAINS,)
    )
    return result


def _store_vehicles(conn, vehicles: list[dict], now: datetime) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO normalized.regional_live_trains (trip_id, line_id, vehicle_id, lat,
                   lon, reported_at, updated_at)
               VALUES (%(trip_id)s, %(line_id)s, %(vehicle_id)s, %(lat)s, %(lon)s,
                       %(reported_at)s, %(now)s)
               ON CONFLICT (trip_id) DO UPDATE SET vehicle_id = EXCLUDED.vehicle_id,
                   lat = EXCLUDED.lat, lon = EXCLUDED.lon, reported_at = EXCLUDED.reported_at,
                   updated_at = EXCLUDED.updated_at""",
            [{**v, "now": now} for v in vehicles],
        )
    return len(vehicles)


def _store_trips(conn, trains: list[dict], stops: list[tuple], now: datetime) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO normalized.regional_live_trains (trip_id, line_id, vehicle_id,
                   delay_seconds, delay_at, cancelled, updated_at)
               VALUES (%(trip_id)s, %(line_id)s, %(vehicle_id)s, %(delay_seconds)s,
                       %(delay_at)s, %(cancelled)s, %(now)s)
               ON CONFLICT (trip_id) DO UPDATE SET
                   vehicle_id = COALESCE(EXCLUDED.vehicle_id, regional_live_trains.vehicle_id),
                   delay_seconds = EXCLUDED.delay_seconds, delay_at = EXCLUDED.delay_at,
                   cancelled = EXCLUDED.cancelled, updated_at = EXCLUDED.updated_at""",
            [{**t, "now": now} for t in trains],
        )
        cur.execute("DELETE FROM normalized.regional_live_stops")
        with cur.copy(
            """COPY normalized.regional_live_stops
               (trip_id, station_id, predicted_departure, skipped) FROM STDIN"""
        ) as copy:
            for row in stops:
                copy.write_row(row)
    return len(trains)


def _store_alerts(conn, alerts: list[dict]) -> int:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM normalized.regional_alerts")
        cur.executemany(
            """INSERT INTO normalized.regional_alerts (alert_id, effect, header, description,
                   url, line_ids, station_ids, active_from, active_until)
               VALUES (%(alert_id)s, %(effect)s, %(header)s, %(description)s, %(url)s,
                       %(line_ids)s, %(station_ids)s, %(active_from)s, %(active_until)s)
               ON CONFLICT (alert_id) DO NOTHING""",
            alerts,
        )
    return len(alerts)


def healthy(conn, now: datetime) -> list[str]:
    """Live regional feeds without a successful poll in the last two minutes (alerts:
    ten)."""
    limits = {"vehicles": timedelta(minutes=2), "trips": timedelta(minutes=2),
              "alerts": timedelta(minutes=10)}  # fmt: skip
    ok = {
        r["feed"]
        for r in conn.execute(
            "SELECT feed, succeeded_at FROM ops.regional_polls WHERE succeeded_at IS NOT NULL"
        )
        if r["succeeded_at"] > now - limits[r["feed"]]
    }
    return [f"regional {feed}" for feed in limits if feed not in ok]
