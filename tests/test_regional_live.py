"""Live regional trains: parsing Metrolinx's feeds, storing them and placing trains."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from google.transit import gtfs_realtime_pb2 as pb

from tests.test_regional import EXHIBITION, UNION, at, idx  # noqa: F401 (fixture)
from transit import regional_live
from transit.db import connect
from transit.http import FetchError
from transit.ttc import realtime
from transit.web import regional as web_regional
from transit.web.app import create_app

LINES = {"01-LW"}
STATIONS = {"UN", "EX", "MI"}
NOW = at("08:05:30")


def message(fill) -> bytes:
    m = pb.FeedMessage()
    m.header.gtfs_realtime_version = "2.0"
    m.header.timestamp = int(NOW.timestamp())
    fill(m)
    return m.SerializeToString()


def vehicles_feed(lat=(UNION[0] + EXHIBITION[0]) / 2, lon=(UNION[1] + EXHIBITION[1]) / 2):
    def fill(m):
        for trip_id, route, la, lo in (("T1", "01-LW", lat, lon), ("B1", "01-21", 43.6, -79.6)):
            e = m.entity.add(id=trip_id)
            e.vehicle.trip.trip_id, e.vehicle.trip.route_id = trip_id, route
            e.vehicle.position.latitude, e.vehicle.position.longitude = la, lo
            e.vehicle.vehicle.id = "620"
            e.vehicle.timestamp = int(NOW.timestamp())

    return message(fill)


def trips_feed(delay=120, cancel_t2=True, skip_mimico=False):
    def fill(m):
        e = m.entity.add(id="T1")
        e.trip_update.trip.trip_id, e.trip_update.trip.route_id = "T1", "01-LW"
        e.trip_update.timestamp = int(NOW.timestamp())
        for station, scheduled in (("EX", "08:07:00"), ("MI", "08:14:00")):
            stu = e.trip_update.stop_time_update.add(stop_id=station)
            stu.departure.delay = delay
            stu.departure.time = int(at(scheduled).timestamp()) + delay
            if skip_mimico and station == "MI":
                stu.schedule_relationship = pb.TripUpdate.StopTimeUpdate.SKIPPED
        e = m.entity.add(id="T2")
        e.trip_update.trip.trip_id, e.trip_update.trip.route_id = "T2", "01-LW"
        if cancel_t2:
            e.trip_update.trip.schedule_relationship = pb.TripDescriptor.CANCELED
        e = m.entity.add(id="B1")  # A bus: left out.
        e.trip_update.trip.trip_id, e.trip_update.trip.route_id = "B1", "01-21"

    return message(fill)


def alerts_feed():
    def fill(m):
        for alert_id, route, stop, header in (
            ("A1", "", "EX", "Exhibition GO elevator out of service"),
            ("A2", "01-LW", "", "Lakeshore West � GO trains every 30 minutes"),
            ("A3", "01-21", "", "Route 21 detour"),
        ):
            alert = m.entity.add(id=alert_id).alert
            alert.informed_entity.add(route_id=route, stop_id=stop)
            alert.header_text.translation.add(text=header, language="en")
            alert.active_period.add(start=int((NOW - timedelta(hours=1)).timestamp()))

    return message(fill)


def test_parsers_keep_rail_only_and_strip_marks():
    snapshot = realtime.decode(vehicles_feed())
    [train] = regional_live.parse_vehicles(snapshot, LINES)
    assert (train["trip_id"], train["vehicle_id"]) == ("T1", "620")
    trains, stops = regional_live.parse_trips(realtime.decode(trips_feed(skip_mimico=True)), LINES)
    assert {t["trip_id"]: (t["delay_seconds"], t["cancelled"]) for t in trains} == {
        "T1": (120, False),
        "T2": (None, True),
    }
    assert [(s[1], s[3]) for s in stops] == [("EX", False), ("MI", True)]
    alerts = regional_live.parse_alerts(realtime.decode(alerts_feed()), LINES, STATIONS)
    assert [(a["header"], a["line_ids"], a["station_ids"]) for a in alerts] == [
        ("Exhibition elevator out of service", [], ["EX"]),
        ("Lakeshore West – trains every 30 minutes", ["01-LW"], []),
    ]


class Client:
    def __init__(self, feeds, fail=()):
        self.feeds, self.fail, self.calls = feeds, fail, []

    def fetch_bytes(self, url, *, host, headers=None, **_):
        self.calls.append((url, host, headers))
        name = next(n for n, path in regional_live.FEEDS.items() if f"/{path}?" in url)
        if name in self.fail:
            raise FetchError("HTTP 503: retries exhausted")
        return self.feeds[name]


@pytest.mark.integration
def test_poll_stores_live_state_and_the_site_shows_it(settings, idx):  # noqa: F811
    feeds = {"vehicles": vehicles_feed(), "trips": trips_feed(), "alerts": alerts_feed()}
    client = Client(feeds)
    with connect(settings.database_url) as conn:
        result = regional_live.poll(conn, client, "k3y", NOW, alerts=True)
        assert {f: r["status"] for f, r in result.items()} == dict.fromkeys(feeds, "ok")
        url, host, headers = client.calls[0]
        assert host == "api.openmetrolinx.com" and url.endswith("VehiclePosition?key=k3y")
        assert headers == {"Accept": "application/x-protobuf"}
        assert regional_live.healthy(conn, NOW) == []
        # A failed feed is recorded and keeps the previous state.
        failing = Client(feeds, fail={"alerts"})
        later = NOW + timedelta(minutes=1)
        assert regional_live.poll(conn, failing, "k3y", later, alerts=True)["alerts"] == {
            "status": "failed",
            "error": "HTTP 503: retries exhausted",
        }
        assert (
            conn.execute("SELECT count(*) AS n FROM normalized.regional_alerts").fetchone()["n"]
            == 2
        )
        assert regional_live.healthy(conn, later + timedelta(minutes=1)) == []
        assert regional_live.healthy(conn, later + timedelta(minutes=11)) == [
            "regional vehicles",
            "regional trips",
            "regional alerts",
        ]

    app = TestClient(create_app(settings, clock=lambda: NOW))
    trains = app.get("/api/regional/trains").json()
    assert trains["source"] == "live" and [t["id"] for t in trains["trains"]] == ["T1"]
    assert trains["alerts"][0]["lines"] == ["LW"]
    pipeline = app.get("/api/pipeline").json()["regional"]
    assert pipeline["live_trains"] == 1 and pipeline["feeds"]["alerts"]["error"]
    exhibition = app.get("/api/regional/stations/EX").json()
    assert exhibition["source"] == "live"
    assert [a["header"] for a in exhibition["alerts"]] == [
        "Exhibition elevator out of service",
        "Lakeshore West – trains every 30 minutes",
    ]


def live_state(**train):
    info = {"lat": None, "lon": None, "reported_at": None, "delay_seconds": None,
            "delay_at": None, "cancelled": False, **train}  # fmt: skip
    return {"trains": {"T1": info}, "stops": {}, "alerts": []}


@pytest.mark.integration
def test_live_trains_are_placed_by_gps_then_delay(idx):  # noqa: F811
    halfway = ((UNION[0] + EXHIBITION[0]) / 2, (UNION[1] + EXHIBITION[1]) / 2)
    # Two minutes late: at 08:05:30 it is where the timetable had it at 08:03:30.
    [late] = web_regional.trains(idx, NOW, live_state(delay_seconds=120, delay_at=NOW))["trains"]
    assert (late["lat"], late["lon"]) == pytest.approx(halfway, abs=1e-4)
    assert late["live"] and late["delay_minutes"] == 2
    assert late["next_arrival"] == at("08:08:00").isoformat()
    # A GPS fix at Exhibition, 30 s old: placed at Exhibition's arrival time plus 30 s.
    gps = live_state(lat=EXHIBITION[0], lon=EXHIBITION[1], reported_at=NOW - timedelta(seconds=30))
    [fix] = web_regional.trains(idx, NOW, gps)["trains"]
    assert fix["at_station"] == "Exhibition" and fix["live"] and fix["delay_minutes"] == -1
    # A fix far from the track is ignored; without a delay the timetable is used.
    far = live_state(lat=44.5, lon=-79.0, reported_at=NOW)
    [scheduled] = web_regional.trains(idx, NOW, far)["trains"]
    assert not scheduled["live"] and scheduled["delay_minutes"] is None
    # Cancelled trains are not shown; their departures are marked.
    assert web_regional.trains(idx, NOW, live_state(cancelled=True))["trains"] == []
    departures = web_regional.station(idx, "UN", at("07:50:00"), live_state(cancelled=True))
    assert departures["departures"][0]["cancelled"] is True


@pytest.mark.integration
def test_departures_use_the_live_prediction(idx):  # noqa: F811
    state = live_state(delay_seconds=120, delay_at=at("08:05:00"))
    state["stops"] = {"T1": {"EX": (at("08:10:00"), False), "MI": (None, True)}}
    exhibition = web_regional.station(idx, "EX", at("08:05:00"), state)
    [t1, *_] = exhibition["departures"]
    assert (t1["trip_id"], t1["minutes"], t1["delay_minutes"], t1["live"]) == ("T1", 5, 3, True)
    assert t1["scheduled"] == at("08:07:00").isoformat()
