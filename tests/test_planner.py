"""Trip planner: its input files and the itineraries the map is given."""

import json
from datetime import UTC, datetime

import httpx
from fastapi.testclient import TestClient

from transit import planner
from transit.config import Settings
from transit.web import planner as web_planner
from transit.web.app import create_app

NOW = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)


class Client:
    def __init__(self, bodies: dict):
        self.bodies = bodies

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None):
        assert url.startswith(f"https://{host}/")
        sink.write(self.bodies[url])
        return b""


def test_inputs_are_replaced_only_when_they_change(tmp_path):
    bodies = {url: name.encode() for name, (url, _, _) in planner.SOURCES.items()}
    assert planner.prepare(tmp_path, Client(bodies))["changed"] is True
    assert (tmp_path / "ttc-gtfs.zip").read_bytes() == b"ttc-gtfs.zip"
    config = json.loads((tmp_path / "build-config.json").read_text())
    assert [f["feedId"] for f in config["transitFeeds"]] == ["ttc", "go", "br", "mi", "yr"]
    assert (tmp_path / "yr-gtfs.zip").exists()
    manifest = (tmp_path / "manifest.json").read_text()

    assert planner.prepare(tmp_path, Client(bodies))["changed"] is False
    assert (tmp_path / "manifest.json").read_text() == manifest  # The graph stays current.

    bodies[planner.regional.GTFS_URL] = b"new timetable"
    assert planner.prepare(tmp_path, Client(bodies))["changed"] is True
    assert (tmp_path / "go-gtfs.zip").read_bytes() == b"new timetable"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []

    # The Toronto-only street extract of earlier versions is removed.
    (tmp_path / "toronto.osm.pbf").write_bytes(b"old")
    assert planner.prepare(tmp_path, Client(bodies))["changed"] is True
    assert not (tmp_path / "toronto.osm.pbf").exists()


def test_polyline_decodes_to_lon_lat():
    # The example from Google's polyline format documentation.
    assert web_planner.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@") == [
        [-120.2, 38.5],
        [-120.95, 40.7],
        [-126.453, 43.252],
    ]


def at(place, lat=43.7, lon=-79.4):
    return {"lat": lat, "lon": lon, **place}


def leg(mode, start, end, *, route=None, headsign=None, frm=None, to=None, **extra):
    return {
        "mode": mode,
        "duration": 600,
        "distance": 800.4,
        "headsign": headsign,
        "start": {"scheduledTime": f"2026-10-14T{start}:00-04:00"},
        "end": {"scheduledTime": f"2026-10-14T{end}:00-04:00"},
        "from": at(frm or {"name": "Origin", "stop": None}),
        "to": at(to or {"name": "Destination", "stop": None}),
        "route": route,
        "legGeometry": {"points": "_p~iF~ps|U_ulLnnqC"},
        **extra,
    }


def itinerary(start, end, legs, walk=300):
    return {
        "node": {
            "start": f"2026-10-14T{start}:00-04:00",
            "end": f"2026-10-14T{end}:00-04:00",
            "numberOfTransfers": 0,
            "walkDistance": walk,
            "legs": legs,
        }
    }


KENNEDY = {"name": "Kennedy GO", "stop": {"gtfsId": "go:KE"}}
PLAN = {
    "planConnection": {
        "routingErrors": [],
        "edges": [
            itinerary(
                "08:40",
                "09:20",
                [
                    leg(
                        "WALK",
                        "08:40",
                        "08:50",
                        to=KENNEDY,
                        steps=[
                            {
                                "distance": 120.4,
                                "relativeDirection": "DEPART",
                                "streetName": "path",
                                "bogusName": True,
                                "lat": 43.73,
                                "lon": -79.26,
                            },
                            {
                                "distance": 300,
                                "relativeDirection": "LEFT",
                                "streetName": "Eglinton Avenue East",
                                "bogusName": False,
                                "lat": 43.731,
                                "lon": -79.262,
                            },
                        ],
                    ),
                    leg(
                        "RAIL",
                        "09:01",
                        "09:20",
                        route={
                            "gtfsId": "go:ST",
                            "shortName": "ST",
                            "longName": "Stouffville",
                            "color": "794500",
                        },
                        headsign="Kennedy GO 09:01 - Union Station GO 09:20",
                        frm=KENNEDY,
                        to={"name": "Union Station GO", "stop": {"gtfsId": "go:UN"}},
                        intermediatePlaces=[
                            {
                                "name": "Danforth GO",
                                "lat": 43.686,
                                "lon": -79.300,
                                "stop": {"gtfsId": "go:DA"},
                                "arrival": {"scheduledTime": "2026-10-14T09:08:00-04:00"},
                            }
                        ],
                    ),
                ],
                walk=950.2,
            ),
            itinerary(
                "08:30",
                "09:10",
                [
                    leg(
                        "SUBWAY",
                        "08:30",
                        "09:10",
                        route={"gtfsId": "ttc:1", "shortName": "1", "longName": "Line 1"},
                        headsign="Line 1 (Yonge-University) towards Finch Station",
                        frm={"name": "Union Station", "stop": {"gtfsId": "ttc:1"}},
                        to={
                            "name": "Finch Station - Subway Platform",
                            "stop": {"gtfsId": "ttc:2"},
                        },
                    )
                ],
            ),
        ],
    }
}


def test_itineraries_are_shaped_for_the_map_without_regional_brand_names():
    first, second = web_planner.itineraries(PLAN)
    assert first["end"] < second["end"]  # Earliest arrival first.
    [subway] = first["legs"]
    assert first["seconds"] == 2400 and subway["network"] == "ttc"
    assert subway["headsign"] == "Finch Station" and subway["color"] is None
    assert subway["to"] == "Finch Station"  # Platform suffix dropped.
    walk, train = second["legs"]
    assert walk["network"] is None and walk["from"] is None and walk["to"] == "Kennedy"
    assert train["network"] == "regional" and train["route"] == "ST"
    assert train["headsign"] == "Union Station" and train["to"] == "Union Station"
    assert train["color"] == "#794500" and train["path"][0] == [-120.2, 38.5]
    assert second["walk_metres"] == 950
    # Walking turns, with unnamed ways left unnamed; stops passed on the ride.
    assert [(s["action"], s["street"], s["metres"]) for s in walk["steps"]] == [
        ("depart", None, 120),
        ("left", "Eglinton Avenue East", 300),
    ]
    assert walk["stops"] == [] and train["steps"] == []
    assert train["stops"] == [
        {"name": "Danforth", "point": [-79.3, 43.686], "arrival": "2026-10-14T09:08:00-04:00"}
    ]
    assert train["from_stop_id"] == "KE" and walk["from_stop_id"] is None
    assert train["to_stop_id"] == "UN" and walk["to_stop_id"] is None
    assert subway["from_point"] == [-79.4, 43.7]


def planner_app(handler):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    app = create_app(
        Settings("postgresql://unused/db", planner_url="http://otp:8080"),
        clock=lambda: NOW,
        planner_client=client,
    )
    return TestClient(app)


def test_plan_endpoint_passes_the_trip_and_checks_its_area_and_time():
    sent = []

    def handler(request):
        if request.url.path == "/otp/":
            return httpx.Response(200)
        sent.append(json.loads(request.content)["variables"])
        return httpx.Response(200, json={"data": PLAN})

    web = planner_app(handler)
    assert web.get("/api/plan/status").json()["available"] is True
    trip = {"from": "43.7764,-79.2575", "to": "43.6453,-79.3806"}
    body = web.get("/api/plan", params={**trip, "at": "2026-10-14T17:00", "arrive": "true"}).json()
    assert len(body["itineraries"]) == 2 and "Metrolinx" in body["attribution"]
    origin = sent[0]["origin"]["location"]["coordinate"]
    assert origin == {"latitude": 43.7764, "longitude": -79.2575}
    # A time without an offset is Toronto time.
    assert sent[0]["dateTime"] == {"latestArrival": "2026-10-14T17:00:00-04:00"}
    assert sent[0]["preferences"] == {"accessibility": {"wheelchair": {"enabled": False}}}
    # Without the database, trips still come back, just without entrances or outages.
    assert "outages" not in body["itineraries"][0]
    web.get("/api/plan", params={**trip, "step_free": "true"})
    assert sent[1]["preferences"]["accessibility"]["wheelchair"]["enabled"] is True

    barrie = web.get("/api/plan", params={**trip, "from": "44.39,-79.69"})
    assert barrie.status_code == 422
    next_spring = web.get("/api/plan", params={**trip, "at": "2027-03-01T08:00"})
    assert next_spring.status_code == 422
    assert len(sent) == 2


def test_plan_endpoint_reports_a_planner_that_is_down():
    def handler(request):
        raise httpx.ConnectError("refused")

    web = planner_app(handler)
    assert web.get("/api/plan/status").json()["available"] is False
    trip = {"from": "43.7764,-79.2575", "to": "43.6453,-79.3806"}
    assert web.get("/api/plan", params=trip).status_code == 503


def test_modes_become_planner_modes():
    assert web_planner.modes_input(web_planner.DEFAULT_MODES) == {
        "direct": ["WALK"],
        "transit": {
            "access": ["WALK"],
            "egress": ["WALK"],
            "transfer": ["WALK"],
            "transit": [{"mode": m} for m in ("BUS", "RAIL", "SUBWAY", "TRAM")],
        },
    }
    bikes = web_planner.modes_input(frozenset({"subway", "bike_share"}))
    assert bikes["direct"] == ["WALK", "BICYCLE_RENTAL"]
    assert bikes["transit"]["egress"] == ["WALK", "BICYCLE_RENTAL"]
    assert bikes["transit"]["transit"] == [{"mode": "SUBWAY"}]
    # Nothing but walking (and Bike Share, if on).
    assert web_planner.modes_input(frozenset()) == {"direct": ["WALK"], "directOnly": True}


def test_leaving_out_one_bus_network_excludes_its_routes():
    sent = []

    def handler(request):
        if request.url.path == "/otp/":
            return httpx.Response(200)
        body = json.loads(request.content)
        if "routes(transportModes" in body["query"]:
            routes = [{"gtfsId": "ttc:7"}, {"gtfsId": "go:21"}, {"gtfsId": "yr:98"}]
            return httpx.Response(200, json={"data": {"routes": routes}})
        sent.append(body["variables"])
        return httpx.Response(200, json={"data": PLAN})

    web_planner._bus_routes.clear()
    web = planner_app(handler)
    trip = {"from": "43.7764,-79.2575", "to": "43.6453,-79.3806"}
    web.get("/api/plan", params={**trip, "modes": "subway,bus"})
    assert sent[0]["preferences"]["transit"] == {"filters": [{"exclude": [{"routes": ["go:21"]}]}]}
    web.get("/api/plan", params={**trip, "modes": "regional_bus"})
    excluded = sent[1]["preferences"]["transit"]["filters"][0]["exclude"][0]["routes"]
    assert excluded == ["ttc:7", "yr:98"]  # Neighbouring agencies' buses are local buses.
    # Both bus networks, or neither: no route filter.
    web.get("/api/plan", params={**trip, "modes": "bus,regional_bus"})
    web.get("/api/plan", params={**trip, "modes": ""})
    assert "transit" not in sent[2]["preferences"] and "transit" not in sent[3]["preferences"]
    assert sent[3]["modes"] == {"direct": ["WALK"], "directOnly": True}
    assert web.get("/api/plan", params={**trip, "modes": "subway,car"}).status_code == 422


def test_bike_share_rides_are_named_by_their_stations():
    ride = leg(
        "BICYCLE",
        "08:40",
        "08:52",
        rentedBike=True,
        frm={
            "name": "bike",
            "stop": None,
            "vehicleRentalStation": {"name": "Queen St W / Spadina Ave"},
        },
        to={"name": "bike", "stop": None, "vehicleRentalStation": {"name": "Union Station"}},
    )
    [trip] = web_planner.itineraries(
        {"planConnection": {"routingErrors": [], "edges": [itinerary("08:40", "08:52", [ride])]}}
    )
    [bike] = trip["legs"]
    assert bike["mode"] == "bicycle" and bike["rented"] and bike["network"] is None
    assert (bike["from"], bike["to"]) == ("Queen St W / Spadina Ave", "Union Station")


def test_neighbouring_agency_legs_are_named_in_normal_case():
    bus = leg(
        "BUS",
        "08:40",
        "09:05",
        route={"gtfsId": "yr:98", "shortName": "098", "longName": "YONGE", "color": None},
        headsign="98 YONGE SB - FINCH TERMINAL",
        frm={"name": "YONGE / 16TH AVENUE", "stop": {"gtfsId": "yr:1234"}},
        to={"name": "FINCH GO BUS TERMINAL", "stop": {"gtfsId": "yr:5678"}},
    )
    [trip] = web_planner.itineraries(
        {"planConnection": {"routingErrors": [], "edges": [itinerary("08:40", "09:05", [bus])]}}
    )
    [ride] = trip["legs"]
    assert (ride["network"], ride["route"], ride["route_name"]) == ("yr", "98", "Yonge")
    assert ride["from"] == "Yonge / 16th Avenue" and ride["from_stop_id"] == "1234"


def test_live_times_replace_the_timetable_where_a_feed_gives_them():
    live = leg(
        "BUS",
        "09:00",
        "09:10",
        route={"gtfsId": "ttc:7", "shortName": "7", "longName": "Bathurst", "color": None},
    )
    live["start"]["estimated"] = {"time": "2026-10-14T09:04:00-04:00"}
    live["end"]["estimated"] = {"time": "2026-10-14T09:13:00-04:00"}
    timetable = leg(
        "BUS",
        "09:20",
        "09:30",
        route={"gtfsId": "ttc:29", "shortName": "29", "longName": "Dufferin", "color": None},
    )
    timetable["start"]["estimated"] = None
    late, scheduled = web_planner._leg(live), web_planner._leg(timetable)
    assert late["start"] == "2026-10-14T09:04:00-04:00" and late["delay_minutes"] == 4
    assert late["end"] == "2026-10-14T09:13:00-04:00"
    assert scheduled["start"] == "2026-10-14T09:20:00-04:00" and scheduled["delay_minutes"] is None
    assert web_planner._leg(leg("WALK", "09:00", "09:05"))["delay_minutes"] is None


def test_planner_reads_live_trip_feeds_that_need_no_key():
    # The planner logs each feed's URL, so Metrolinx's, which carries its key, is left out.
    updaters = [u for u in planner.ROUTER_CONFIG["updaters"] if u["type"] == "stop-time-updater"]
    assert {u["feedId"] for u in updaters} == {"ttc"} | set(planner.agencies.AGENCIES)
    assert not any("key" in u["url"] for u in updaters)
