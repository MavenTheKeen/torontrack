"""Public API: pure helpers offline, endpoints against a seeded *_test database."""

import re
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.test_ingestion_integration import FixtureClient
from tests.test_planner import itinerary, leg
from tests.ttc_helpers import T0, alert_feed, static_zip, trip_feed
from transit.bikeshare.ingestion import run
from transit.config import Settings
from transit.db import connect, migrations
from transit.ttc import static_gtfs, store
from transit.web import pipeline, queries
from transit.web.app import RateLimiter, TTLCache, client_key, create_app


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_cache_expires_after_ttl():
    clock, calls = Clock(), []
    cache = TTLCache(15, clock=clock)
    assert cache.get("k", lambda: calls.append(1) or "a") == "a"
    clock.now = 14
    assert cache.get("k", lambda: "b") == "a"
    clock.now = 15.1
    assert cache.get("k", lambda: "c") == "c"


def test_rate_limit_is_per_client_sliding_minute():
    clock = Clock()
    limiter = RateLimiter(2, clock=clock)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")
    clock.now = 60.1
    assert limiter.allow("a")


def train(direction, progress, position, in_service=True):
    return {
        "direction_id": direction,
        "_progress": progress,
        "position": position,
        "in_service": in_service,
    }


def test_long_gap_needs_to_be_well_above_scheduled_headway():
    trains = [train(0, 1000, 10), train(0, 820, 8), train(0, 100, 2), train(0, 0, 0, False)]
    result = queries.gaps(trains, {0: 180})
    assert [(g["seconds"], g["long"]) for g in result] == [(180, False), (720, True)]
    # Without a schedule for this hour, nothing is flagged.
    assert not any(g["long"] for g in queries.gaps(trains, {}))


def test_line_display_name_drops_brackets_and_spaced_hyphens():
    assert queries.display_name("Line 2 (Bloor - Danforth)", "2") == "Line 2 Bloor–Danforth"
    assert queries.display_name("Line 1 (Yonge-University)", "1") == "Line 1 Yonge–University"
    assert queries.display_name("Sheppard", "4") == "Sheppard"


def test_line_status_takes_most_severe_active_service_alert():
    line_list = [{"id": "1", "name": "Line 1"}, {"id": "2", "name": "Line 2"}]
    alerts = [
        {
            "id": "a",
            "kind": "service",
            "timing": "active",
            "lines": ["1"],
            "status": "delays",
            "header": "Delays",
        },
        {
            "id": "b",
            "kind": "service",
            "timing": "active",
            "lines": ["1"],
            "status": "no_service",
            "header": "No service",
        },
        {
            "id": "c",
            "kind": "service",
            "timing": "upcoming",
            "lines": ["2"],
            "status": "no_service",
            "header": "Weekend closure",
        },
        {
            "id": "d",
            "kind": "accessibility",
            "timing": "active",
            "lines": ["2"],
            "status": "accessibility",
            "header": "Elevator",
        },
    ]
    fresh = {"stale": False}
    one, two = queries.line_statuses(line_list, alerts, fresh)
    assert (one["status"], one["label"], one["summary"]) == (
        "no_service",
        "No service",
        "No service",
    )
    assert (two["status"], two["label"]) == ("normal", "Normal service")
    stale = queries.line_statuses(line_list, alerts, {"stale": True})
    assert {s["status"] for s in stale} == {"unknown"}


@pytest.fixture
def client(settings):
    with connect(settings.database_url) as conn:
        static_gtfs.load(conn, static_gtfs.parse(static_zip()), "test://gtfs", T0)
        trips = trip_feed(
            T0,
            [
                {
                    "trip_id": "1",
                    "vehicle": "101",
                    "route_id": "1",
                    "stops": [("13864", 2, 0), ("13806", 3, 150), ("13807", 4, 270)],
                },
                {"trip_id": "2", "vehicle": "102", "route_id": "1", "stops": [("13863", 3, 80)]},
            ],
        )
        alerts = alert_feed(
            T0,
            [
                {
                    "id": "10",
                    "effect": "SIGNIFICANT_DELAYS",
                    "routes": ["1"],
                    "text": "Line 1: Delays of up to 10 minutes near College",
                },
                {
                    "id": "11",
                    "effect": "REDUCED_SERVICE",
                    "routes": ["2"],
                    "text": "Line 2: There will be no subway service this weekend",
                },
            ],
        )
        access = alert_feed(
            T0,
            [
                {
                    "id": "20",
                    "effect": "ACCESSIBILITY_ISSUE",
                    "stops": ["13864"],
                    "text": "Bloor-Yonge: Elevator out of service",
                }
            ],
        )
        for feed, payload in [
            ("trips_subway", trips),
            ("alerts_subway", alerts),
            ("alerts_accessibility", access),
        ]:
            snapshot_id, _ = store.store_snapshot(conn, feed, "test://", payload, T0)
            store.normalize_snapshot(conn, snapshot_id)
    run(settings, "bikes", client=FixtureClient())
    app = create_app(settings, clock=lambda: T0 + timedelta(seconds=10))
    return TestClient(app)


@pytest.mark.integration
def test_status_and_alerts(client):
    status = client.get("/api/status")
    assert status.status_code == 200
    assert status.headers["cache-control"] == "public, max-age=15"
    by_line = {s["line"]: s for s in status.json()["lines"]}
    assert by_line["1"]["status"] == "delays"
    assert by_line["1"]["source"] == "reported"
    # An advance notice is listed as upcoming but does not change today's status.
    assert by_line["2"]["status"] == "normal"
    alerts = client.get("/api/alerts").json()
    assert [a["id"] for a in alerts["active"]] == ["10"]
    assert [a["id"] for a in alerts["upcoming"]] == ["11"]
    assert alerts["upcoming"][0]["advance_notice"] is True
    assert [a["id"] for a in alerts["accessibility"]] == ["20"]
    assert "Toronto Transit Commission" in alerts["attribution"]


@pytest.mark.integration
def test_line_view_places_trains_between_stations(client):
    body = client.get("/api/lines/1").json()
    assert [s["name"] for s in body["stations"]] == [
        "Rosedale",
        "Bloor-Yonge",
        "Wellesley",
        "College",
    ]
    trains = {t["train_id"]: t for t in body["trains"]}
    # 101: Bloor-Yonge predicted now (TTC quirk); next is Wellesley 140 s away, longer than
    # the 120 s scheduled run, so it has not left Bloor-Yonge yet.
    assert trains["101"]["at_station"] is True
    assert trains["101"]["station"] == "Bloor-Yonge"
    assert trains["101"]["position"] == 1
    # 102 northbound towards Bloor-Yonge, between Wellesley and Bloor-Yonge.
    assert trains["102"]["direction_id"] == 1
    assert trains["102"]["at_station"] is False
    assert trains["102"]["position"] == pytest.approx(1 + 70 / 120, abs=0.001)
    assert all(not k.startswith("_") for t in body["trains"] for k in t)
    assert client.get("/api/lines/9").status_code == 404
    assert client.get("/api/lines/abc").status_code == 404


@pytest.mark.integration
def test_station_page_has_arrivals_alerts_and_bikes(client):
    body = client.get("/api/stations/bloor-yonge").json()
    station = body["station"]
    assert station["lines"] == ["1", "2"]
    south = next(d for d in station["directions"] if d["stop_id"] == "13864")
    assert south["towards"] == "Vaughan Metropolitan Centre"
    assert [a["train_id"] for a in south["arrivals"]] == ["101"]
    assert [a["id"] for a in body["alerts"]] == ["20"]
    assert {s["line"] for s in body["line_status"]} == {"1", "2"}
    assert len(body["bike_share"]["docks"]) == 2
    assert body["bike_share"]["docks"][0]["distance_m"] >= 0
    assert client.get("/api/stations/nope").status_code == 404
    assert client.get("/api/stations/" + "x" * 80).status_code == 404


def test_a_station_is_disrupted_only_by_alerts_that_cover_it():
    def alert(lines, stops, kind="service", timing="active"):
        return {"kind": kind, "timing": timing, "lines": lines, "stop_ids": stops}

    closed = alert(["1"], ["13793", "13794"])  # Eglinton to Lawrence
    # Union (platforms 14404, 14405) is on Line 1 but outside the closed section.
    assert queries.station_disrupted_lines(["1"], ["14404", "14405"], [closed]) == []
    assert queries.station_disrupted_lines(["1"], ["13793", "13800"], [closed]) == ["1"]
    # An alert naming no stations covers the whole line; other lines are unaffected.
    assert queries.station_disrupted_lines(["1", "2"], ["x"], [alert(["2"], [])]) == ["2"]
    # Elevator alerts and upcoming closures do not change service status.
    others = [alert(["1"], [], kind="accessibility"), alert(["1"], [], timing="upcoming")]
    assert queries.station_disrupted_lines(["1"], ["x"], others) == []


@pytest.mark.integration
def test_map_has_line_paths_stations_and_every_dock(client):
    response = client.get("/api/map", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    body = response.json()
    line_one = next(line for line in body["lines"] if line["id"] == "1")
    assert line_one["status"]["status"] == "delays"
    assert len(line_one["path"]) == len(client.get("/api/lines/1").json()["stations"])
    bloor = next(s for s in body["stations"] if s["key"] == "bloor-yonge")
    assert bloor["lines"] == ["1", "2"] and -80 < bloor["lon"] < -79
    # Elevator alerts are attached through the station's platforms, and nowhere else.
    assert bloor["outages"] == ["Bloor-Yonge: Elevator out of service"]
    assert all(not s["outages"] for s in body["stations"] if s["key"] != "bloor-yonge")
    assert "stop_ids" not in bloor and isinstance(bloor["disrupted"], list)
    docks = body["bike_share"]["docks"]
    assert len(docks) == 2
    for dock in docks:
        # A dock without a current report carries no availability claim.
        assert dock["current"] or (dock["bikes"] is None and dock["docks"] is None)


@pytest.mark.integration
def test_trains_endpoint_places_every_line_s_trains(client):
    body = client.get("/api/trains").json()
    line_one = next(line for line in body["lines"] if line["id"] == "1")
    assert line_one["stations"] == [s["key"] for s in client.get("/api/lines/1").json()["stations"]]
    assert line_one["trains"] == client.get("/api/lines/1").json()["trains"]
    assert {line["id"] for line in body["lines"]} == {"1", "2"}


@pytest.mark.integration
def test_map_stations_carry_entrances_and_bike_parking(client):
    bloor = next(s for s in client.get("/api/map").json()["stations"] if s["key"] == "bloor-yonge")
    # No OpenStreetMap layers are built in tests, so both are empty but present.
    assert bloor["entrances"] == [] and bloor["bike_parking"] is None


@pytest.mark.integration
def test_trip_plans_carry_elevator_outages_at_their_stations(client, settings, tmp_path):
    def handler(request):
        if request.url.path == "/otp/":
            return httpx.Response(200)
        ride = leg(
            "SUBWAY",
            "08:30",
            "08:40",
            route={"gtfsId": "ttc:1", "shortName": "1", "longName": "Line 1"},
            frm={"name": "Union Station", "stop": {"gtfsId": "ttc:unknown"}},
            to={"name": "Bloor-Yonge Station", "stop": {"gtfsId": "ttc:13864"}},
        )
        plan = {"routingErrors": [], "edges": [itinerary("08:30", "08:40", [ride])]}
        return httpx.Response(200, json={"data": {"planConnection": plan}})

    app = create_app(
        replace(settings, map_dir=str(tmp_path), planner_url="http://otp:8080"),
        clock=lambda: T0 + timedelta(seconds=10),
        planner_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    trip = {"from": "43.7764,-79.2575", "to": "43.6453,-79.3806", "step_free": "true"}
    [planned] = TestClient(app).get("/api/plan", params=trip).json()["itineraries"]
    [ride] = planned["legs"]
    assert ride["outages"] == [
        {
            "station": ride["outages"][0]["station"],
            "alerts": ["Bloor-Yonge: Elevator out of service"],
        }
    ]
    assert planned["outages"] == ride["outages"]


@pytest.mark.integration
def test_health_and_page(client):
    health = client.get("/health").json()
    assert health["status"] == "ok"
    page = client.get("/")
    assert page.status_code == 200
    assert "Toronto Transit Commission" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/static/app.js").status_code == 200


def wait(order, waited, next_in=None, platform="Southbound", route="1", direction=0):
    now = T0
    return {
        "route_id": route,
        "direction_id": direction,
        "stop_order": order,
        "stop_id": f"s{order}",
        "towards": "Vaughan Metropolitan Centre",
        "station_name": f"Station {order}",
        "station_key": f"station-{order}",
        "platform": platform,
        "last_arrival": now - timedelta(seconds=waited),
        "next_arrival": None if next_in is None else now + timedelta(seconds=next_in),
    }


HEADWAYS = {"1": {0: 180}}  # Long gap threshold: max(360, 480) = 480 s.


def test_detection_needs_two_adjacent_silent_platforms():
    waits = [wait(2, 300), wait(3, 700), wait(4, 660, next_in=200), wait(5, 100)]
    incidents = queries.detected_delays(waits, HEADWAYS, [], T0)
    assert incidents["1"] == [
        {
            "direction_id": 0,
            "towards": "Vaughan Metropolitan Centre",
            "station": "Station 3",
            "station_key": "station-3",
            "platforms_affected": 2,
            "waited_seconds": 700,
            "scheduled_headway_seconds": 180,
            "next_train_seconds": None,
            "message": "No southbound train at Station 3 in 11 min",
        }
    ]
    # One silent platform alone is not enough.
    assert queries.detected_delays([wait(3, 700), wait(5, 700)], HEADWAYS, [], T0) == {}


def test_detection_is_suppressed_by_imminent_train_gap_or_unknown_schedule():
    waits = [wait(3, 700, next_in=30), wait(4, 700, next_in=45)]
    assert queries.detected_delays(waits, HEADWAYS, [], T0) == {}
    silent = [wait(3, 700), wait(4, 700)]
    gap = [(T0 - timedelta(minutes=8), T0 - timedelta(minutes=5))]
    assert queries.detected_delays(silent, HEADWAYS, gap, T0) == {}
    assert queries.detected_delays(silent, {}, [], T0) == {}
    towards = [wait(3, 700, platform="Subway"), wait(4, 700, platform="Subway")]
    message = queries.detected_delays(towards, HEADWAYS, [], T0)["1"][0]["message"]
    assert message == "No train towards Vaughan Metropolitan Centre at Station 3 in 11 min"


def test_detected_status_only_when_ttc_reports_nothing():
    line_list = [{"id": "1", "name": "Line 1"}, {"id": "2", "name": "Line 2"}]
    alerts = [
        {
            "id": "a",
            "kind": "service",
            "timing": "active",
            "lines": ["2"],
            "status": "delays",
            "header": "Delays",
        }
    ]
    detected = {
        "1": [{"message": "No southbound train at X in 11 min"}],
        "2": [{"message": "No westbound train at Y in 9 min"}],
    }
    one, two = queries.line_statuses(line_list, alerts, {"stale": False}, detected)
    assert one["status"] == "normal"
    assert one["detected"]["label"] == "Possible delay, not confirmed by TTC"
    assert one["detected"]["source"] == "detected"
    assert two["status"] == "delays" and two["detected"] is None


def gap(route, direction, start_min, end_min, scheduled_min, station, train="T1", hour=12):
    start = T0 + timedelta(minutes=start_min)
    end = T0 + timedelta(minutes=end_min)
    seconds = (end - start).total_seconds()
    return {
        "route_id": route,
        "direction_id": direction,
        "service_hour": hour,
        "towards": "Finch" if direction == 0 else "Vaughan",
        "station_key": station.lower(),
        "station_name": station,
        "train_id": train,
        "gap_start": start,
        "gap_end": end,
        "gap_seconds": seconds,
        "scheduled_headway_seconds": scheduled_min * 60,
        "excess_seconds": seconds - scheduled_min * 60,
    }


def test_overlapping_gaps_on_a_line_are_one_delay_ranked_by_excess():
    gaps = [
        # One disruption seen by three train pairs, in both directions.
        gap("1", 1, 0, 18, 4, "Wilson", train="A"),
        gap("1", 0, 3, 18, 4, "Lawrence West", train="B"),
        gap("1", 1, 3, 17, 4, "Sheppard West", train="C"),
        # Train A again, ending a gap behind a different train further along: one train.
        gap("1", 1, 5, 16, 4, "Downsview Park", train="A"),
        # A separate delay later on the same line, and one on another line meanwhile.
        gap("1", 0, 60, 75, 3, "St Clair"),
        gap("2", 0, 5, 20, 5, "Kipling"),
    ]
    incidents = queries.gap_incidents(gaps)
    assert [(i["route_id"], i["station_name"], i["trains_delayed"]) for i in incidents] == [
        ("1", "Wilson", 3),
        ("1", "St Clair", 1),
        ("2", "Kipling", 1),
    ]
    first = incidents[0]
    assert first["both_directions"] and first["excess_seconds"] == 14 * 60
    assert (first["start"], first["end"]) == (T0, T0 + timedelta(minutes=18))


def test_pipeline_verdict_is_by_time_since_last_success():
    limit = timedelta(minutes=2)
    assert pipeline.verdict(T0 - timedelta(seconds=30), limit, T0) == "ok"
    assert pipeline.verdict(T0 - timedelta(minutes=5), limit, T0) == "delayed"
    assert pipeline.verdict(T0 - timedelta(minutes=9), limit, T0) == "failing"
    assert pipeline.verdict(None, limit, T0) == "failing"


def test_pipeline_checks_completeness_and_data_age_not_only_the_last_run():
    assert pipeline.completeness(90, 96) == "ok"
    assert pipeline.completeness(67, 96) == "delayed"  # Ran recently, missed a third.
    assert pipeline.completeness(40, 96) == "failing"
    ok, failing = pipeline.WEATHER_DATA_OK, pipeline.WEATHER_DATA_FAILING
    age = pipeline.data_age_verdict
    assert age(T0 - timedelta(hours=21), ok, failing, T0) == "ok"
    assert age(T0 - timedelta(hours=50), ok, failing, T0) == "delayed"
    assert age(T0 - timedelta(hours=80), ok, failing, T0) == "failing"
    assert pipeline.worst("ok", "delayed") == "delayed"


@pytest.mark.integration
def test_pipeline_endpoint_reports_each_part(client):
    body = client.get("/api/pipeline").json()
    # The fixture stores snapshots directly, without the poller, so no poll has succeeded.
    assert [f["feed"] for f in body["ttc"]["feeds"]] == list(pipeline.FEED_LABELS)
    assert {f["status"] for f in body["ttc"]["feeds"]} == {"failing"}
    assert body["overall"] == "failing"
    assert body["bikeshare"]["succeeded_24h"] == 1
    assert {c["check_name"] for c in body["bikeshare"]["quality_checks"]} >= {
        "station_status_coverage"
    }
    assert body["dbt"]["status"] == "failing" and body["dbt"]["latest"] is None
    assert (body["weather"]["status"], body["weather"]["hours"]) == ("failing", 0)
    assert body["data"]["schema_version"] == migrations()[-1][0]


@pytest.mark.integration
def test_reliability_endpoint_reports_analytics_not_built(client, settings):
    with connect(settings.database_url) as conn:
        conn.execute("DROP TABLE IF EXISTS analytics.ttc_long_gaps")
    assert client.get("/api/reliability").json()["available"] is False


@pytest.mark.integration
def test_report_endpoint_reports_analytics_not_built(client, settings):
    with connect(settings.database_url) as conn:
        conn.execute("DROP TABLE IF EXISTS analytics.ttc_station_reliability")
    assert client.get("/api/report").json()["available"] is False


def test_installable_app_files_are_served_and_complete():
    app_client = TestClient(create_app(Settings("postgresql://unused/db")))
    worker = app_client.get("/sw.js")
    assert worker.status_code == 200 and "javascript" in worker.headers["content-type"]
    assert worker.headers["cache-control"] == "no-cache"  # New versions are picked up.
    manifest = app_client.get("/static/manifest.webmanifest")
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    # Everything the manifest and the worker's shell list must exist, or install fails.
    icons = [icon["src"] for icon in manifest.json()["icons"]]
    shell = re.findall(r'"(/static/[^"]+)"', worker.text)
    assert len(shell) > 5
    for path in icons + shell:
        assert app_client.get(path).status_code == 200, path
    font = app_client.get("/static/fonts/atkinson-hyperlegible-next-latin.woff2")
    assert font.headers["content-type"] == "font/woff2"


def test_security_headers_and_hsts_only_over_https():
    plain = TestClient(create_app(Settings("postgresql://unused/db")))
    page = plain.get("/")
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["permissions-policy"].startswith("geolocation=(self), camera=()")
    assert page.headers["cross-origin-opener-policy"] == "same-origin"
    assert page.headers["cross-origin-resource-policy"] == "same-origin"
    assert "strict-transport-security" not in page.headers
    secure = TestClient(create_app(Settings("postgresql://unused/db")), base_url="https://t")
    assert secure.get("/").headers["strict-transport-security"] == "max-age=31536000"


def test_rate_limit_groups_an_ipv6_connection_and_forgets_oldest_clients():
    assert client_key("2001:db8:1:2:aaaa::1") == client_key("2001:db8:1:2:bbbb::9")
    assert client_key("2001:db8:1:2::1") != client_key("2001:db8:1:3::1")
    assert client_key("::ffff:203.0.113.7") == client_key("203.0.113.7") == "203.0.113.7"
    assert client_key("unknown") == "unknown"
    limiter = RateLimiter(1, clock=Clock())
    assert limiter.allow("2001:db8::1") and not limiter.allow("2001:db8::2")
    limiter.MAX_CLIENTS = 3
    for host in ("10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.4"):
        assert limiter.allow(host)
    assert len(limiter.hits) == 3 and client_key("2001:db8::1") not in limiter.hits


def test_head_robots_and_favicon():
    client = TestClient(create_app(Settings("postgresql://unused/db")))
    assert {client.head(p).status_code for p in ("/", "/robots.txt", "/favicon.ico")} == {200}
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and "Disallow: /api/" in robots.text
    icon = client.get("/favicon.ico")
    assert icon.status_code == 200 and icon.headers["content-type"] == "image/png"
