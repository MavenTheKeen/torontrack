"""Public read-only API and page. Run: uvicorn transit.web.app:app

Responses are cached in memory for 15 s, so load on PostgreSQL does not grow with
visitors. A small per-IP limit protects the process. Browsers only talk to this
service; it never calls the TTC.
"""

import gzip
import hashlib
import ipaddress
import json
import logging
import mimetypes
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from transit import events, places, roads
from transit.agencies import AGENCIES
from transit.config import Settings
from transit.db import connect
from transit.web import (
    access,
    agencies,
    agenda,
    bikes,
    pipeline,
    planner,
    queries,
    regional,
    report,
    surface,
)

logger = logging.getLogger(__name__)
CACHE_SECONDS = 15
# Train and vehicle positions are refreshed this often, matching the 10 s collector.
LIVE_SECONDS = 5
STATIC = files("transit.web").joinpath("static")
# The site's font; slim Python images have no system type table that lists it.
mimetypes.add_type("font/woff2", ".woff2")
ATTRIBUTION = "Data: Toronto Transit Commission. Predictions may be inaccurate."
BIKES_ATTRIBUTION = (
    "Bike Share Toronto ridership: Toronto Parking Authority. Contains information licensed "
    "under the Open Government Licence – Toronto."
)
EVENTS_ATTRIBUTION = (
    "Events: City of Toronto festivals and events calendar. Contains information licensed "
    "under the Open Government Licence – Toronto."
)
AGENDA_ATTRIBUTION = EVENTS_ATTRIBUTION + " Concerts, games and shows: Ticketmaster."
SURFACE_ATTRIBUTION = " ".join([ATTRIBUTION, *(a.credit for a in AGENCIES.values())])
# A neighbouring agency's stop or route: its key, a colon and its own ID (transit.agencies).
AGENCY_ID = re.compile(r"[a-z]{2}:[A-Za-z0-9_-]{1,24}")
ROADS_ATTRIBUTION = (
    "Road restrictions: City of Toronto. Contains information licensed under the "
    "Open Government Licence – Toronto."
)
PLANNER_ATTRIBUTION = (
    "Trip plans: OpenTripPlanner with TTC schedules (Open Government Licence – Toronto), "
    "regional schedules (Data used in this product or service is provided with the "
    "permission of Metrolinx.) and streets © OpenStreetMap contributors. Planned from "
    "published timetables, not live predictions."
)
REGIONAL_ATTRIBUTION = (
    "Data used in this product or service is provided with the permission of Metrolinx. "
    "Times are from the published timetable, not live train positions."
)


ROBOTS = "User-agent: *\nDisallow: /api/\nDisallow: /map-data/\n"
PERMISSIONS_POLICY = ", ".join(
    [
        "geolocation=(self)",
        *(
            f"{feature}=()"
            for feature in (
                "camera",
                "microphone",
                "payment",
                "usb",
                "serial",
                "bluetooth",
                "hid",
                "midi",
                "display-capture",
                "browsing-topics",
            )
        ),
    ]
)


class Encoded:
    """A cached response body, encoded to JSON the first time it is served and kept that
    way, rather than on every request: encoding the 1.7 MB stop list took 0.2 s."""

    def __init__(self, data):
        self.data, self.body = data, None

    def response(self) -> Response:
        if self.body is None:
            self.body = json.dumps(
                jsonable_encoder(self.data),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        return Response(self.body, media_type="application/json")


class TTLCache:
    """Values kept for `seconds`. Requests that miss the same key at the same time wait for
    one computation rather than each running it: when the live vehicles expire, every
    open map asks for them within the same few seconds."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.seconds, self.clock = seconds, clock
        self.items: dict = {}
        self.building: dict = {}
        self.lock = threading.Lock()

    def _fresh(self, key):
        hit = self.items.get(key)
        return hit if hit and hit[0] > self.clock() else None

    def get(self, key, compute):
        with self.lock:
            if hit := self._fresh(key):
                return hit[1]
            building = self.building.setdefault(key, threading.Lock())
        with building:
            with self.lock:
                if hit := self._fresh(key):
                    return hit[1]
            now = self.clock()
            try:
                value = compute()
            except BaseException:
                with self.lock:
                    self.building.pop(key, None)
                raise
            with self.lock:
                self.items[key] = (now + self.seconds, value)
                self.building.pop(key, None)
                if len(self.items) > 500:  # Unknown station keys cannot grow memory unbounded.
                    self.items = {k: v for k, v in self.items.items() if v[0] > now}
        return value


def client_key(host: str) -> str:
    """The rate limit's key for a client address. An IPv6 home or phone connection is
    given a whole /64, so one client could otherwise pick a fresh address per request."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if address.version == 4:
        return str(address)
    if address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(ipaddress.ip_network(f"{address}/64", strict=False))


class RateLimiter:
    """Sliding one-minute window per client (client_key)."""

    # Clients tracked at once; past this, the least recently seen are forgotten, so a
    # flood of distinct addresses cannot grow memory without bound.
    MAX_CLIENTS = 20_000

    def __init__(self, limit: int, clock=time.monotonic):
        self.limit, self.clock = limit, clock
        self.hits: OrderedDict[str, deque] = OrderedDict()
        self.lock = threading.Lock()

    def allow(self, client: str) -> bool:
        key = client_key(client)
        now = self.clock()
        with self.lock:
            window = self.hits.setdefault(key, deque())
            self.hits.move_to_end(key)
            while window and window[0] <= now - 60:
                window.popleft()
            if len(window) >= self.limit:
                return False
            window.append(now)
            while len(self.hits) > self.MAX_CLIENTS:
                self.hits.popitem(last=False)
            return True


def create_app(
    settings: Settings,
    clock=lambda: datetime.now(UTC),
    planner_client: httpx.Client | None = None,
) -> FastAPI:
    app = FastAPI(title="Torontrack", docs_url=None, redoc_url=None, openapi_url=None)
    # Open-data files written by `transit export`; optional, served at /data/.
    export_dir = settings.export_dir
    # The street map built by `transit map-tiles`; optional, served at /map-data/.
    map_dir = settings.map_dir
    # Station entrances and bike parking from `transit osm-layers`, in the same folder.
    osm = access.Layers(map_dir)
    cache = TTLCache(CACHE_SECONDS)
    live = TTLCache(LIVE_SECONDS)
    # The routes' usual pace by hour changes slowly; read from its dbt table hourly.
    slow = TTLCache(3600)
    limiter = RateLimiter(settings.web_rate_limit_per_minute)
    # Each trip plan is a routing search, far costlier than a cached read.
    plan_limiter = RateLimiter(settings.web_plan_rate_limit_per_minute)
    # The trip planner (docs/planner.md) is optional; without it /api/plan answers 503.
    planner_url = settings.planner_url
    if planner_url and planner_client is None:
        planner_client = httpx.Client(timeout=12)
    planner_state = TTLCache(60)
    static_cache = {}
    static_lock = threading.Lock()
    # The surface network is loaded after the subway schedule, so it is cached separately
    # and only once it exists for the current version.
    surface_cache = {}

    def db_url():
        if not settings.database_url:
            raise RuntimeError("Set DATABASE_URL")
        return settings.database_url

    def schedule(conn):
        """Per-feed-version data that only changes when a new schedule is loaded."""
        version = queries.feed_version(conn)
        with static_lock:
            if static_cache.get("version") != version:
                static_cache.clear()
                static_cache.update(
                    version=version,
                    lines=queries.lines(conn, version),
                    stations=queries.all_stations(conn, version),
                    segments=queries.segment_seconds(conn, version),
                    network=queries.map_network(conn, version),
                )
            return dict(static_cache)

    def overview(conn, now):
        static = schedule(conn)
        state = queries.feed_state(conn)
        alerts = queries.current_alerts(conn, now)
        alerts_fresh = queries.freshness(state, "alerts_subway", now, queries.ALERTS_STALE)
        # Light rail alerts come from their own feed (alerts_light_rail).
        alerts_fresh["light_rail_stale"] = queries.freshness(
            state, "alerts_light_rail", now, queries.ALERTS_STALE
        )["stale"]
        detected = {}
        if not queries.freshness(state, "trips_subway", now, queries.PREDICTIONS_STALE)["stale"]:
            headways = {
                line["id"]: queries.scheduled_headways(conn, static["version"], line["id"], now)
                for line in static["lines"]
            }
            detected = queries.detected_delays(
                queries.platform_waits(conn, static["version"], now),
                headways,
                queries.collection_gaps(conn, now),
                now,
            )
        statuses = queries.line_statuses(static["lines"], alerts, alerts_fresh, detected)
        return static, state, alerts, statuses

    def cached(key, build, attribution=ATTRIBUTION, store=None, data=False):
        """The endpoint's response, built at most once per cache period; with `data`, the
        body itself, for use inside another endpoint."""

        def compute():
            now = clock()
            with connect(db_url()) as conn:
                body = build(conn, now)
            return Encoded({"generated_at": now, "attribution": attribution, **body})

        try:
            entry = (store or cache).get(key, compute)
        except queries.NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return entry.data if data else entry.response()

    # Responses that change only with a schedule: (key) -> (tag, body, gzipped body).
    by_schedule_store: dict[str, tuple] = {}

    def schedule_tag(conn) -> str:
        """Identifies the TTC's and the neighbouring agencies' loaded schedule versions."""
        try:
            ttc = queries.feed_version(conn)
        except queries.NotFound:
            ttc = None
        versions = json.dumps([ttc, agencies.versions(conn)], sort_keys=True)
        return hashlib.sha256(versions.encode()).hexdigest()[:16]

    def by_schedule(request: Request, key, build, attribution=ATTRIBUTION):
        """A response that changes only with a new schedule: built, encoded and compressed
        once per schedule version, and sent with an ETag so a browser that has it already
        gets a 304 instead of the whole body again (together about 800 KB compressed)."""
        tag = cache.get("schedule-tag", lambda: with_conn(schedule_tag))
        etag = f'"{key}-{tag}"'
        headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        sent = request.headers.get("if-none-match", "")
        if etag in (t.strip().removeprefix("W/") for t in sent.split(",")):
            return Response(status_code=304, headers=headers)
        stored = by_schedule_store.get(key)
        if stored is None or stored[0] != tag:
            body = cached(f"{key}:{tag}", build, attribution).body
            stored = (tag, body, gzip.compress(body, 9))
            by_schedule_store[key] = stored
        if "gzip" in request.headers.get("accept-encoding", ""):
            return Response(
                stored[2],
                media_type="application/json",
                headers={**headers, "Content-Encoding": "gzip"},
            )
        return Response(stored[1], media_type="application/json", headers=headers)

    def with_conn(read):
        with connect(db_url()) as conn:
            return read(conn)

    # The map response lists every Bike Share dock (~200 KB); compressed it is a fraction.
    # Level 6 rather than 9: half the CPU on the live vehicles (7 ms instead of 15 ms per
    # response) for a body 2% larger.
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        client = request.client.host if request.client else "unknown"
        if request.url.path.startswith("/api/") and not limiter.allow(client):
            return JSONResponse(
                {"detail": "Too many requests"}, status_code=429, headers={"Retry-After": "60"}
            )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # Location is the only device feature the site uses ("near me" and trip guidance).
        response.headers["Permissions-Policy"] = PERMISSIONS_POLICY
        # No other site may open this one in a window it can script, or embed its files.
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        # Sent only over HTTPS, as the standard requires; behind a proxy the scheme comes
        # from its X-Forwarded-Proto. The site's own Caddy sends the same header.
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if "cache-control" in response.headers:
            pass  # The endpoint chose its own.
        elif request.url.path.startswith("/api/") and response.status_code == 200:
            response.headers["Cache-Control"] = f"public, max-age={CACHE_SECONDS}"
        elif request.url.path.startswith("/data/") and response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=3600"
        elif request.url.path.startswith("/map-data/") and response.status_code in (200, 206):
            # Rebuilt rarely; the map reads it in ranges, which browsers cache too.
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif request.url.path.startswith(("/static/vendor/", "/static/fonts/")):
            # The map library (1.1 MB) and the font change only with an upgrade.
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif request.url.path.startswith("/static/"):
            # The site's own files are checked on every load (a 304 while unchanged), so a
            # deploy reaches visitors at once rather than after a browser's guessed
            # freshness, which could pair an old app.js with a new API.
            response.headers["Cache-Control"] = "no-cache"
        return response

    # HEAD too, as uptime monitors and link previews use it.
    @app.api_route("/health", methods=["GET", "HEAD"])
    def health():
        try:
            with connect(db_url()) as conn:
                state = queries.feed_state(conn)
                version = queries.feed_version(conn)
        except queries.NotFound:
            return {"status": "no_data", "schedule_version": None, "predictions_as_of": None}
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        now = clock()
        trips = queries.freshness(state, "trips_subway", now, queries.PREDICTIONS_STALE)
        return {
            "status": "degraded" if trips["stale"] else "ok",
            "schedule_version": version[:12],
            "predictions_as_of": trips["as_of"],
        }

    @app.get("/api/status")
    def status():
        def build(conn, now):
            static, state, alerts, statuses = overview(conn, now)
            return {
                "alerts_as_of": queries.freshness(
                    state, "alerts_subway", now, queries.ALERTS_STALE
                ),
                "lines": statuses,
            }

        return cached("status", build)

    @app.get("/api/alerts")
    def alerts():
        def build(conn, now):
            _, state, current, _ = overview(conn, now)
            return {
                "alerts_as_of": queries.freshness(
                    state, "alerts_subway", now, queries.ALERTS_STALE
                ),
                "active": [
                    a for a in current if a["kind"] == "service" and a["timing"] == "active"
                ],
                "upcoming": [a for a in current if a["timing"] == "upcoming"],
                "accessibility": [
                    a for a in current if a["kind"] == "accessibility" and a["timing"] == "active"
                ],
            }

        return cached("alerts", build)

    @app.get("/api/lines")
    def lines():
        def build(conn, now):
            static, _, _, statuses = overview(conn, now)
            by_line = {s["line"]: s for s in statuses}
            return {
                "lines": [{**line, "status": by_line[line["id"]]} for line in static["lines"]],
                "stations": static["stations"],
            }

        return cached("lines", build)

    @app.get("/api/lines/{route_id}")
    def line(route_id: str):
        if not route_id.isdigit() or len(route_id) > 3:
            raise HTTPException(status_code=404, detail="Unknown line")

        def build(conn, now):
            static, state, alerts, statuses = overview(conn, now)
            meta = next((x for x in static["lines"] if x["id"] == route_id), None)
            if meta is None:
                raise queries.NotFound(f"Unknown line {route_id}")
            stations = queries.line_stations(conn, static["version"], route_id)
            predictions = queries.line_predictions(conn, static["version"], route_id, now)
            trains = queries.train_positions(
                predictions, route_id, stations, static["segments"], now
            )
            headways = queries.scheduled_headways(conn, static["version"], route_id, now)
            gaps = queries.gaps(trains, headways)
            return {
                "line": meta,
                "status": next(s for s in statuses if s["line"] == route_id),
                "predictions_as_of": queries.freshness(
                    state, "trips_subway", now, queries.PREDICTIONS_STALE
                ),
                "stations": [{"key": s["key"], "name": s["name"]} for s in stations],
                "trains": [{k: v for k, v in t.items() if not k.startswith("_")} for t in trains],
                "gaps": gaps,
                "scheduled_headway_seconds": {d: round(h) for d, h in headways.items()},
            }

        return cached(f"line:{route_id}", build)

    @app.get("/api/stations/{key}")
    def station(key: str):
        if len(key) > 60 or not key.replace("-", "").isalnum():
            raise HTTPException(status_code=404, detail="Unknown station")

        def build(conn, now):
            static, state, alerts, statuses = overview(conn, now)
            data = queries.station(conn, static["version"], key, now)
            stops = set(data.pop("stop_ids"))
            return {
                "station": data,
                "predictions_as_of": queries.freshness(
                    state, "trips_subway", now, queries.PREDICTIONS_STALE
                ),
                "line_status": [s for s in statuses if s["line"] in data["lines"]],
                "alerts": [a for a in alerts if stops & set(a["stop_ids"])],
                "bike_share": queries.nearest_bikes(conn, data["lat"], data["lon"], now),
            }

        return cached(f"station:{key}", build)

    @app.get("/api/map")
    def network_map():
        def build(conn, now):
            static, _, alerts, statuses = overview(conn, now)
            by_line = {s["line"]: s for s in statuses}
            network = static["network"]
            outages = access.station_outages(
                network["stations"], [a for a in alerts if a["kind"] == "accessibility"]
            )
            entrances, bike_parking = osm.by_station(network["stations"], static["version"])
            stations = []
            for s in network["stations"]:
                station = {k: v for k, v in s.items() if k != "stop_ids"}
                station["outages"] = outages.get(s["key"], [])
                station["disrupted"] = queries.station_disrupted_lines(
                    s["lines"], s.get("stop_ids", []), alerts
                )
                station["entrances"] = entrances.get(s["key"], [])
                station["bike_parking"] = bike_parking.get(s["key"])
                stations.append(station)
            return {
                "lines": [
                    {**line, "status": by_line[line["id"]], "path": network["paths"][line["id"]]}
                    for line in static["lines"]
                ],
                "stations": stations,
                "bike_share": queries.bike_docks(conn, now),
            }

        return cached("map", build)

    @app.get("/api/trains")
    def trains():
        """Estimated positions of every train, for the map: a position is a fractional
        index into the line's station list, as on the line diagrams."""

        def build(conn, now):
            static, state = schedule(conn), queries.feed_state(conn)
            lines = []
            for meta in static["lines"]:
                stations = queries.line_stations(conn, static["version"], meta["id"])
                predictions = queries.line_predictions(conn, static["version"], meta["id"], now)
                positions = queries.train_positions(
                    predictions, meta["id"], stations, static["segments"], now
                )
                lines.append(
                    {
                        "id": meta["id"],
                        "stations": [s["key"] for s in stations],
                        "directions": meta["directions"],
                        "trains": [
                            {k: v for k, v in t.items() if not k.startswith("_")} for t in positions
                        ],
                    }
                )
            return {
                "predictions_as_of": queries.freshness(
                    state, "trips_subway", now, queries.PREDICTIONS_STALE
                ),
                "lines": lines,
            }

        return cached("trains", build, store=live)

    def surface_network(conn):
        version = queries.feed_version(conn)
        with static_lock:
            if surface_cache.get("version") != version:
                network = surface.network(conn, version)
                if not network["stops"]:
                    return network
                surface_cache.clear()
                surface_cache.update(version=version, network=network)
            return surface_cache["network"]

    def ttc_part(read, empty):
        """The TTC's share of a combined bus endpoint, or `empty` before its schedule is
        loaded, so neighbouring agencies still show."""
        try:
            return read()
        except queries.NotFound:
            return empty

    @app.get("/api/surface")
    def surface_stops(request: Request):
        """Every bus and streetcar route and stop, the TTC's and neighbouring agencies';
        changes only with a new schedule."""

        def build(conn, now):
            ttc = ttc_part(lambda: surface_network(conn), {"routes": [], "stops": []})
            others = agencies.network(conn)
            return {
                **ttc,
                "routes": ttc["routes"] + others["routes"],
                "stops": ttc["stops"] + others["stops"],
            }

        return by_schedule(request, "surface", build, SURFACE_ATTRIBUTION)

    def matched(conn, now):
        """Vehicles matched to their route patterns, the TTC's then other agencies',
        shared by the vehicle and stop endpoints for one cache period."""

        def build():
            ttc = ttc_part(
                lambda: surface.matched_vehicles(conn, queries.feed_version(conn), now), []
            )
            return ttc + agencies.matched_vehicles(conn, now)

        return live.get("matched", build)

    @app.get("/api/vehicles")
    def vehicles():
        def build(conn, now):
            try:
                version = queries.feed_version(conn)
                pace = slow.get(f"pace:{version}", lambda: surface.typical_pace(conn, version))
            except (psycopg.Error, queries.NotFound):
                pace = {}  # Without a usual pace, vehicles move at the timetable's.
            return surface.vehicles(matched(conn, now), now, pace)

        return cached("vehicles", build, attribution=SURFACE_ATTRIBUTION, store=live)

    @app.get("/api/patterns")
    def surface_patterns(request: Request):
        """Every bus and streetcar pattern's stops with timetable times, for moving vehicles
        between reports; changes only with a new schedule."""

        def build(conn, now):
            ttc = ttc_part(lambda: surface.patterns(conn, queries.feed_version(conn)), {})
            return {"patterns": {**ttc.get("patterns", {}), **agencies.patterns(conn)}}

        return by_schedule(request, "patterns", build)

    @app.get("/api/stops/{stop_id}")
    def bus_stop(stop_id: str):
        other = AGENCY_ID.fullmatch(stop_id) is not None
        if not other and (not stop_id.isalnum() or len(stop_id) > 12):
            raise HTTPException(status_code=404, detail="Unknown stop")

        def build(conn, now):
            if other:
                data = agencies.stop(conn, stop_id, matched(conn, now), now)
            else:
                version = queries.feed_version(conn)
                data = surface.stop(conn, version, stop_id, matched(conn, now), now)
            if data is None:
                raise queries.NotFound(f"Unknown stop {stop_id}")
            return data

        return cached(f"stop:{stop_id}", build, attribution=SURFACE_ATTRIBUTION)

    @app.get("/api/routes/{route_id}")
    def surface_route(route_id: str):
        other = AGENCY_ID.fullmatch(route_id) is not None
        if not other and (not route_id.isalnum() or len(route_id) > 6):
            raise HTTPException(status_code=404, detail="Unknown route")

        def build(conn, now):
            if other:
                data = agencies.route(conn, route_id)
            else:
                data = surface.route(conn, queries.feed_version(conn), route_id)
            if data is None:
                raise queries.NotFound(f"Unknown route {route_id}")
            return data

        return cached(f"route:{route_id}", build, attribution=SURFACE_ATTRIBUTION)

    @app.get("/api/docks/{station_id}/trips")
    def dock_trips(station_id: str):
        """A dock's trips over the last 12 months of published ridership."""
        if not station_id.isalnum() or len(station_id) > 12:
            raise HTTPException(status_code=404, detail="Unknown dock")

        def build(conn, now):
            data = bikes.dock(conn, station_id)
            if data is None:
                raise queries.NotFound(f"No trips for dock {station_id}")
            return data

        return cached(f"dock-trips:{station_id}", build, BIKES_ATTRIBUTION)

    @app.get("/api/events")
    def city_events():
        """City of Toronto festivals and events from today through the next 7 days."""
        return cached(
            "events",
            lambda conn, now: {"events": events.upcoming(conn, now)},
            EVENTS_ATTRIBUTION,
        )

    @app.get("/api/agenda")
    def event_agenda():
        """The Events page: city events and Ticketmaster events for the next 14 days."""

        def build(conn, now):
            days = agenda.window(now)
            stations = [{**s, "kind": "subway"} for s in schedule(conn)["network"]["stations"]]
            idx = regional_index(conn, now)
            if idx:
                stations += [
                    {**s, "kind": "regional", "key": None}
                    for s in regional.network(idx)["stations"]
                ]
            return agenda.build(
                agenda.city_rows(conn, days[0], days[-1]),
                agenda.ticketed_rows(conn, days[0], days[-1]),
                stations,
                days,
            )

        return cached("agenda", build, AGENDA_ATTRIBUTION)

    @app.get("/api/roads")
    def road_restrictions():
        """Construction zones and road closures whose dates cover now."""

        def build(conn, now):
            return {"restrictions": roads.active(conn, now)}

        return cached("roads", build, ROADS_ATTRIBUTION)

    def regional_index(conn, now):
        version = regional.current_version(conn, now.astimezone(regional.TORONTO).date())
        return regional.index(conn, version) if version else None

    @app.get("/api/regional")
    def regional_network():
        """Regional train lines and stations; changes only with a new schedule."""

        def build(conn, now):
            idx = regional_index(conn, now)
            return regional.network(idx) if idx else {"lines": [], "stations": []}

        return cached("regional", build, REGIONAL_ATTRIBUTION)

    @app.get("/api/regional/trains")
    def regional_trains():
        def build(conn, now):
            idx = regional_index(conn, now)
            if not idx:
                return {"source": "timetable", "trains": [], "alerts": []}
            return regional.trains(idx, now, regional.live(conn, now))

        return cached("regional-trains", build, REGIONAL_ATTRIBUTION, store=live)

    @app.get("/api/regional/stations/{station_id}")
    def regional_station(station_id: str):
        if not station_id.isalnum() or len(station_id) > 6:
            raise HTTPException(status_code=404, detail="Unknown station")

        def build(conn, now):
            idx = regional_index(conn, now)
            data = regional.station(idx, station_id, now, regional.live(conn, now)) if idx else None
            if data is None:
                raise queries.NotFound(f"Unknown station {station_id}")
            return data

        return cached(f"regional-station:{station_id}", build, REGIONAL_ATTRIBUTION)

    def planner_up() -> bool:
        def probe():
            try:
                return planner_client.get(f"{planner_url.rstrip('/')}/otp/", timeout=2).is_success
            except httpx.HTTPError:
                return False

        return bool(planner_url) and planner_state.get("up", probe)

    def lat_lon(text: str, name: str) -> tuple[float, float]:
        try:
            lat, lon = (float(part) for part in text.split(","))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"{name} must be lat,lon") from None
        if not planner.inside(lat, lon):
            raise HTTPException(status_code=422, detail=f"{name} is outside the planner's area")
        return lat, lon

    @app.get("/api/plan/status")
    def plan_status():
        west, south, east, north = planner.EXTENT
        return {"available": planner_up(), "extent": [west, south, east, north]}

    def plan_context(conn, now):
        """What trip plans are annotated with: each subway platform's station, the stations'
        entrances, and the elevator and escalator alerts at each station."""
        static = schedule(conn)
        stations = static["network"]["stations"]
        alerts = [a for a in queries.current_alerts(conn, now) if a["kind"] == "accessibility"]
        entrances, _ = osm.by_station(stations, static["version"])
        return {
            "station_of_stop": {
                stop: {"key": s["key"], "name": s["name"]}
                for s in stations
                for stop in s.get("stop_ids", [])
            },
            "entrances": entrances,
            "outages": access.station_outages(stations, alerts),
        }

    @app.get("/api/plan")
    def trip_plan(
        request: Request,
        origin: str = Query(alias="from", max_length=40),
        to: str = Query(max_length=40),
        at: datetime | None = None,
        arrive: bool = False,
        step_free: bool = False,
        modes: str | None = Query(None, max_length=80),
    ):
        """Transit and walking itineraries between two points, from published timetables,
        with station entrances, exits and elevator outages for each subway ride."""
        client = request.client.host if request.client else "unknown"
        if not plan_limiter.allow(client):
            return JSONResponse(
                {"detail": "Too many trip plans"}, status_code=429, headers={"Retry-After": "60"}
            )
        start, end = lat_lon(origin, "from"), lat_lon(to, "to")
        chosen = planner.DEFAULT_MODES
        if modes is not None:
            chosen = frozenset(m for m in modes.split(",") if m)
            if not chosen <= set(planner.MODES):
                raise HTTPException(
                    status_code=422, detail=f"modes must be from {', '.join(planner.MODES)}"
                )
        now = clock()
        when = at or now
        if when.tzinfo is None:
            when = when.replace(tzinfo=regional.TORONTO)
        if not now - timedelta(days=1) <= when <= now + timedelta(days=60):
            raise HTTPException(status_code=422, detail="at must be within the next 60 days")
        if not planner_url:
            raise HTTPException(status_code=503, detail="Trip planner is not running")
        try:
            body = planner.plan(
                planner_client, planner_url, start, end, when, arrive, step_free, chosen
            )
        except planner.PlannerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        try:
            context = cached("plan-context", plan_context, data=True)
        except (psycopg.Error, RuntimeError, HTTPException):
            # Without the database, plans still work; they just lack entrances and outages.
            logger.warning("Trip plan annotations unavailable", exc_info=True)
        else:
            access.annotate(
                body["itineraries"],
                context["station_of_stop"],
                context["entrances"],
                context["outages"],
                step_free,
            )
        return {"generated_at": now, "attribution": PLANNER_ATTRIBUTION, **body}

    @app.get("/api/places")
    def place_search(
        q: str = Query(min_length=2, max_length=80),
        near: str | None = Query(None, max_length=40),
    ):
        """Addresses, named places and intersections matching `q`, nearest to `near`
        (lat,lon) first among equals; from `transit osm-layers` (docs/planner.md)."""
        path = Path(map_dir) / places.FILE_NAME if map_dir else None
        if path is None or not path.is_file():
            return {"available": False, "places": []}
        point = None
        if near:
            try:
                lat, lon = (float(part) for part in near.split(","))
                point = (lon, lat)
            except ValueError:
                raise HTTPException(status_code=422, detail="near must be lat,lon") from None
        return {
            "available": True,
            "places": places.search(path, q, near=point),
            "attribution": "© OpenStreetMap contributors",
        }

    @app.get("/api/pipeline")
    def pipeline_health():
        return cached("pipeline", lambda conn, now: pipeline.pipeline_status(conn, now))

    @app.get("/api/reliability")
    def reliability():
        def build(conn, now):
            try:
                return {"available": True, **queries.reliability(conn, now)}
            except psycopg.errors.UndefinedTable:
                # Reliability tables appear after the first dbt build.
                return {"available": False}

        return cached("reliability", build)

    @app.get("/api/report")
    def reliability_report():
        def build(conn, now):
            try:
                return {"available": True, **report.report(conn)}
            except psycopg.errors.UndefinedTable:
                return {"available": False}

        return cached("report", build)

    @app.get("/api/datasets")
    def datasets():
        def load():
            index = Path(export_dir) / "index.json" if export_dir else None
            if index is None or not index.is_file():
                return {"available": False}
            return {"available": True, **json.loads(index.read_text(encoding="utf-8"))}

        return cache.get("datasets", load)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index():
        return FileResponse(STATIC.joinpath("index.html"), headers={"Cache-Control": "no-cache"})

    # Crawlers may index the page but not call the API or fetch the street map's tiles,
    # which would only spend the rate limit.
    @app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
    def robots():
        return PlainTextResponse(ROBOTS, headers={"Cache-Control": "public, max-age=86400"})

    # Browsers and feed readers ask for /favicon.ico whatever the page says; PNG is fine.
    @app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
    def favicon():
        return FileResponse(
            STATIC.joinpath("icon-192.png"),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    # The service worker must be served from the root to control the whole site, and
    # always revalidated so a new version is picked up on the next visit.
    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return FileResponse(
            STATIC.joinpath("sw.js"),
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/static/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(
            STATIC.joinpath("manifest.webmanifest"), media_type="application/manifest+json"
        )

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    if export_dir:
        # check_dir=False: the folder may not exist until the first export runs.
        app.mount("/data", StaticFiles(directory=export_dir, check_dir=False), name="data")
    if map_dir:
        app.mount("/map-data", StaticFiles(directory=map_dir, check_dir=False), name="map-data")
    return app


app = create_app(Settings.from_env(require_database=False))
