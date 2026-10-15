"""Neighbouring bus agencies: their stops, routes and live bus positions, shown on the map
beside the TTC's (docs/agencies.md).

Each agency publishes static GTFS and GTFS-Realtime vehicle positions. The static feed is
loaded like the TTC's surface network (transit.ttc.surface.parse: routes, stops and stop
patterns with scheduled times), keeping one current version per agency. The live feed is
polled every POLL_SECONDS and each bus's latest report is kept; the site matches buses to
their route's patterns by geometry, as it does TTC buses, and IDs are prefixed with the
agency's key ("br:511") because route numbers repeat across agencies.

Licences: Brampton Transit's data is CC BY 4.0 (credit required); MiWay's is under the
City of Mississauga's Terms of Use, which require that redistributed data carry the terms'
URL. Neither allows implying endorsement or using the agencies' logos, so the map uses its
own colours and plain names.
"""

import hashlib
import json
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from transit import locks
from transit.bikeshare.parsing import FeedValidationError
from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc import realtime, surface


@dataclass(frozen=True)
class Agency:
    key: str
    name: str
    static_url: str
    vehicles_url: str
    credit: str

    def host(self, url: str) -> str:
        return url.split("/")[2]


AGENCIES = {
    "br": Agency(
        key="br",
        name="Brampton Transit",
        static_url="https://www.arcgis.com/sharing/rest/content/items/a355aabd5a8c490186bdce559c9c75fb/data",
        vehicles_url="https://gtfs-rt-merge.prod.bt-cadavl.com/BramptonTransit/GTFS/merged_VehiclePosition.pb",
        credit="Brampton Transit data: City of Brampton, licensed under CC BY 4.0.",
    ),
    "mi": Agency(
        key="mi",
        name="MiWay",
        static_url="https://www.miapp.ca/GTFS/google_transit.zip",
        vehicles_url="https://www.miapp.ca/GTFS_RT/Vehicle/VehiclePositions.pb",
        credit=(
            "MiWay data: City of Mississauga, under its Terms of Use "
            "(www5.mississauga.ca/research_catalogue/CityofMississauga_TermsofUse.pdf)."
        ),
    ),
}
STATIC_MAX_BYTES = 80 << 20
POLL_SECONDS = 15
# A bus's last report is kept this long after it stops reporting.
KEEP_REPORTS = timedelta(hours=1)
LOG_EVERY_SECONDS = 3600


DIRECTIONS = {"nb": "North", "sb": "South", "eb": "East", "wb": "West"}


def towards(headsign: str) -> str:
    """A destination from a headsign: '71 W Sheridan To Plymouth Loop' -> 'Plymouth Loop',
    '51 HEREFORD EAST' -> 'Hereford East', 'Queen Eb-Fccc' -> 'Queen East'. The leading
    route number is dropped, then anything up to 'to' or 'towards'; a trailing ' - code'
    goes, bound abbreviations become directions, all capitals become title case, and the
    regional rail brand is dropped from station names (as in transit.regional)."""
    text = re.sub(r"^\s*[0-9]+[A-Za-z]?\s+", "", headsign.strip())
    text = re.split(r"\s+(?:to|towards)\s+", text, maxsplit=1, flags=re.IGNORECASE)[-1]
    text = re.sub(r"\s+-\s+\S+$", "", text.strip())
    if text.isupper():
        text = text.title()
    text = re.sub(
        r"\b(nb|sb|eb|wb)\b(?:-\w+)?",
        lambda m: DIRECTIONS[m.group(1).lower()],
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+GO(\s+Station)?$", "", text, flags=re.IGNORECASE).strip()
    return text or headsign.strip()


# ---- Static network ----------------------------------------------------------------------


def load(conn, agency: Agency, feed: surface.SurfaceFeed, sha: str, now: datetime) -> dict:
    """Store a parsed network as the agency's only version, in one transaction."""
    version = sha[:16]
    with conn.transaction(), conn.cursor() as cur:
        if conn.execute(
            "SELECT 1 FROM normalized.agency_versions WHERE agency = %s AND feed_version = %s",
            (agency.key, version),
        ).fetchone():
            return {"status": "unchanged", "feed_version": version, **feed.stats}
        cur.execute(
            "DELETE FROM normalized.agency_versions WHERE agency = %s", (agency.key,)
        )  # Cascades to the old version's routes, stops and patterns.
        cur.execute(
            """INSERT INTO normalized.agency_versions
               (agency, feed_version, feed_sha256, source_url, loaded_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (agency.key, version, sha, agency.static_url, now),
        )
        cur.executemany(
            """INSERT INTO normalized.agency_routes
               (agency, feed_version, route_id, short_name, long_name) VALUES (%s,%s,%s,%s,%s)""",
            [
                (agency.key, version, r["route_id"], r["short_name"], r["long_name"])
                for r in feed.routes
            ],
        )
        with cur.copy(
            """COPY normalized.agency_stops
               (agency, feed_version, stop_id, stop_code, name, lat, lon) FROM STDIN"""
        ) as copy:
            for s in feed.stops:
                stop = (s["stop_id"], s["stop_code"], s["name"], s["lat"], s["lon"])
                copy.write_row((agency.key, version, *stop))
        cur.executemany(
            """INSERT INTO normalized.agency_patterns
               (agency, feed_version, pattern_id, route_id, direction_id, towards, trips)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            [
                (
                    agency.key,
                    version,
                    p["pattern_id"],
                    p["route_id"],
                    p["direction_id"],
                    p["towards"],
                    p["trips"],
                )
                for p in feed.patterns
            ],  # fmt: skip
        )
        with cur.copy(
            """COPY normalized.agency_pattern_stops
               (agency, feed_version, pattern_id, stop_order, stop_id, offset_seconds) FROM STDIN"""
        ) as copy:
            for row in feed.pattern_stops:
                copy.write_row((agency.key, version, *row))
    return {"status": "loaded", "feed_version": version, **feed.stats}


def refresh(database_url: str, client: FeedClient, agencies=None) -> dict:
    """Load each agency's static GTFS if it changed. Every agency is tried; a failure is
    recorded and reported after the others have been loaded."""
    results = {}
    for agency in agencies or AGENCIES.values():
        started = datetime.now(UTC)
        result: dict = {"status": "failed"}
        try:
            body = client.fetch_bytes(
                agency.static_url, host=agency.host(agency.static_url), max_bytes=STATIC_MAX_BYTES
            )
            sha = hashlib.sha256(body).hexdigest()
            feed = surface.parse(body, modes={"3": "bus"}, name_towards=towards)
            with connect(database_url) as conn:
                result = load(conn, agency, feed, sha, started)
        except (FetchError, FeedValidationError, KeyError, ValueError) as exc:
            result["error"] = str(exc)[:1000]
        with connect(database_url) as conn:
            conn.execute(
                """INSERT INTO ops.agency_refreshes (agency, started_at, finished_at, status,
                       feed_version, routes, stops, patterns, error)
                   VALUES (%s, %s, now(), %s, %s, %s, %s, %s, %s)""",
                (
                    agency.key,
                    started,
                    result["status"],
                    result.get("feed_version"),
                    result.get("routes"),
                    result.get("stops"),
                    result.get("patterns"),
                    result.get("error"),
                ),  # fmt: skip
            )
        results[agency.key] = result
    return results


# ---- Live positions ----------------------------------------------------------------------


def parse_vehicles(snapshot: realtime.Snapshot) -> list[dict]:
    out = {}
    for entity in snapshot.message.entity:
        if not entity.HasField("vehicle") or not entity.vehicle.HasField("position"):
            continue
        v = entity.vehicle
        vehicle_id = v.vehicle.id or entity.id
        lat, lon = v.position.latitude, v.position.longitude
        if not vehicle_id or not (42.5 < lat < 45 and -81 < lon < -78):
            continue
        reported = (
            datetime.fromtimestamp(v.timestamp, UTC) if v.timestamp else snapshot.feed_timestamp
        )
        out[vehicle_id] = {
            "vehicle_id": vehicle_id,
            "route_id": v.trip.route_id or None,
            "trip_id": v.trip.trip_id or None,
            "lat": lat,
            "lon": lon,
            "bearing": round(v.position.bearing) % 360 if v.position.HasField("bearing") else None,
            "speed_mps": v.position.speed if v.position.HasField("speed") else None,
            "occupancy": realtime.gtfs_realtime_pb2.VehiclePosition.OccupancyStatus.Name(
                v.occupancy_status
            )
            if v.HasField("occupancy_status")
            else None,
            "reported_at": reported,
        }
    return list(out.values())


def poll(conn, client: FeedClient, agency: Agency, now: datetime) -> dict:
    try:
        snapshot = realtime.decode(
            client.fetch_bytes(agency.vehicles_url, host=agency.host(agency.vehicles_url))
        )
        vehicles = parse_vehicles(snapshot)
    except (FetchError, FeedValidationError) as exc:
        conn.execute(
            """INSERT INTO ops.agency_polls (agency, polled_at, status, error)
               VALUES (%s, %s, 'failed', %s)
               ON CONFLICT (agency) DO UPDATE SET polled_at = EXCLUDED.polled_at,
                   status = 'failed', error = EXCLUDED.error""",
            (agency.key, now, str(exc)[:1000]),
        )
        return {"agency": agency.key, "status": "failed", "error": str(exc)[:200]}
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO normalized.agency_vehicles (agency, vehicle_id, route_id, trip_id,
                   lat, lon, bearing, speed_mps, occupancy, reported_at)
               VALUES (%(agency)s, %(vehicle_id)s, %(route_id)s, %(trip_id)s, %(lat)s, %(lon)s,
                       %(bearing)s, %(speed_mps)s, %(occupancy)s, %(reported_at)s)
               ON CONFLICT (agency, vehicle_id) DO UPDATE SET route_id = EXCLUDED.route_id,
                   trip_id = EXCLUDED.trip_id, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                   bearing = EXCLUDED.bearing, speed_mps = EXCLUDED.speed_mps,
                   occupancy = EXCLUDED.occupancy, reported_at = EXCLUDED.reported_at
               WHERE EXCLUDED.reported_at >= agency_vehicles.reported_at""",
            [{**v, "agency": agency.key} for v in vehicles],
        )
        cur.execute(
            "DELETE FROM normalized.agency_vehicles WHERE agency = %s AND reported_at < %s",
            (agency.key, now - KEEP_REPORTS),
        )
        cur.execute(
            """INSERT INTO ops.agency_polls (agency, polled_at, status, vehicles, feed_timestamp)
               VALUES (%s, %s, 'ok', %s, %s)
               ON CONFLICT (agency) DO UPDATE SET polled_at = EXCLUDED.polled_at, status = 'ok',
                   vehicles = EXCLUDED.vehicles, feed_timestamp = EXCLUDED.feed_timestamp,
                   error = NULL""",
            (agency.key, now, len(vehicles), snapshot.feed_timestamp),
        )
    return {"agency": agency.key, "status": "ok", "vehicles": len(vehicles)}


class CollectorBusy(RuntimeError):
    pass


def run_forever(database_url: str, client: FeedClient | None = None, stop=None) -> None:
    """Poll every agency every POLL_SECONDS until stopped. The static networks are loaded
    first if missing, so matching works from the first poll."""
    stop = stop or threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except ValueError:  # Not the main thread (tests).
            pass
    client = client or FeedClient()
    with connect(database_url) as lock_conn:
        if not lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (locks.AGENCY_COLLECTOR,)
        ).fetchone()["locked"]:
            raise CollectorBusy("Another agency collector is already running")
        with connect(database_url) as conn:
            loaded = {
                r["agency"]
                for r in conn.execute("SELECT DISTINCT agency FROM normalized.agency_versions")
            }
        missing = [a for a in AGENCIES.values() if a.key not in loaded]
        if missing:
            _log({"event": "static_gtfs", **refresh(database_url, client, missing)})
        summary: dict = {}
        last_log = time.monotonic()
        while not stop.is_set():
            started = time.monotonic()
            with connect(database_url) as conn:
                for agency in AGENCIES.values():
                    result = poll(conn, client, agency, datetime.now(UTC))
                    counts = summary.setdefault(agency.key, {"ok": 0, "failed": 0})
                    counts[result["status"]] += 1
                    if result["status"] == "failed":
                        _log({"event": "poll_failed", **result})
            if started - last_log >= LOG_EVERY_SECONDS:
                _log({"event": "hourly", "polls": summary})
                summary, last_log = {}, started
            stop.wait(max(0.0, POLL_SECONDS - (time.monotonic() - started)))
    client.close()


def healthy(conn, now: datetime) -> list[str]:
    """Agencies without a successful poll in the last two minutes."""
    ok = {
        r["agency"]
        for r in conn.execute(
            "SELECT agency FROM ops.agency_polls WHERE status = 'ok' AND polled_at > %s",
            (now - timedelta(minutes=2),),
        )
    }
    return sorted(set(AGENCIES) - ok)


def _log(record: dict):
    print(json.dumps({"at": datetime.now(UTC).isoformat(), **record}, default=str), flush=True)
