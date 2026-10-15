"""Neighbouring bus agencies (transit.agencies, transit.web.agencies): loading a static
network, polling live positions, and serving both with prefixed IDs beside the TTC's.

The feeds are small hand-built ones with the structure of Brampton Transit's and MiWay's
(route_type 3, direction_id on every trip, headsigns like theirs), checked against the
real feeds on 2026-10-15.
"""

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.ttc_helpers import surface_trip_feed, vehicle_feed
from transit import agencies
from transit.config import Settings
from transit.db import connect
from transit.web import agencies as web
from transit.web.app import create_app

NOW = datetime(2026, 10, 15, 16, tzinfo=UTC)
BRAMPTON = agencies.AGENCIES["br"]
# Route 1 Queen: three stops east along Queen Street, two minutes apart.
STOPS = [
    ("101", "Queen at Main", 43.6850, -79.7600),
    ("102", "Queen at Kennedy", 43.6870, -79.7470),
    ("103", "Queen at Rutherford", 43.6890, -79.7340),
]


def _csv(header, rows):
    return "\n".join([",".join(header), *(",".join(map(str, r)) for r in rows)]) + "\n"


def static_zip() -> bytes:
    trips, stop_times = [], []
    for n, start in enumerate((8 * 3600, 9 * 3600)):
        trips.append(("1", "WKDY", f"t{n}", "1 Queen Eb-Fccc", 0))
        for order, (stop_id, *_) in enumerate(STOPS):
            t = start + order * 120
            clock = f"{t // 3600:02d}:{t % 3600 // 60:02d}:00"
            stop_times.append((f"t{n}", clock, clock, stop_id, order + 1))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("agency.txt", _csv(["agency_id", "agency_name"], [("BT", "Brampton Transit")]))
        z.writestr(
            "routes.txt",
            _csv(
                ["route_id", "route_short_name", "route_long_name", "route_type"],
                [("1", "1", "Queen", 3)],
            ),  # fmt: skip
        )
        z.writestr(
            "trips.txt",
            _csv(["route_id", "service_id", "trip_id", "trip_headsign", "direction_id"], trips),
        )
        z.writestr(
            "stop_times.txt",
            _csv(
                ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                stop_times,
            ),  # fmt: skip
        )
        z.writestr(
            "stops.txt",
            _csv(
                ["stop_id", "stop_code", "stop_name", "stop_lat", "stop_lon"],
                [(s, f"9{s}", n, lat, lon) for s, n, lat, lon in STOPS],
            ),  # fmt: skip
        )
    return buffer.getvalue()


class Client:
    def __init__(self, live=None):
        self.live = live

    def fetch_bytes(self, url, host, max_bytes=None, sink=None):
        assert host == url.split("/")[2]
        return static_zip() if url == BRAMPTON.static_url else self.live

    def close(self):
        pass


def test_headsigns_become_destinations():
    assert agencies.towards("71 W Sheridan To Plymouth Loop") == "Plymouth Loop"
    assert agencies.towards("51 HEREFORD EAST") == "Hereford East"
    assert agencies.towards("Queen Eb-Fccc") == "Queen East"
    assert agencies.towards("Airport Road North - Nlsd") == "Airport Road North"
    assert agencies.towards("Bramalea Go") == "Bramalea"
    assert agencies.towards("Newmarket Terminal - NB") == "Newmarket Terminal"
    assert agencies.towards("Cornell Bus Terminal via Highway 7 - EB") == (
        "Cornell Bus Terminal via Highway 7"
    )


def test_route_numbers_are_shown_as_riders_see_them():
    assert agencies.route_number("001") == "1"
    assert agencies.route_number("107B") == "107B"
    assert agencies.route_number("blue") == "Blue"
    assert agencies.route_number("A") == "A"


def test_route_names_in_capitals_are_shown_in_normal_case():
    title = agencies.route_title
    assert title("MARKHAM DISTRICT SS VIA CARLTON") == "Markham District SS via Carlton"
    assert title("DR G.W. WILLIAMS SS VIA HENDERSON") == "Dr G.W. Williams SS via Henderson"
    assert title("16TH AVENUE") == "16th Avenue"
    assert title("MCCALLUM") == "McCallum"
    assert title("YONGE (LATE NIGHT)") == "Yonge (Late Night)"
    assert title("VAUGHAN MILLS / WONDERLAND") == "Vaughan Mills / Wonderland"
    assert title("Queen Street") == "Queen Street"


@pytest.mark.integration
def test_a_network_and_live_buses_are_served_with_prefixed_ids(settings):
    first = agencies.refresh(settings.database_url, Client(), [BRAMPTON])
    assert first["br"]["status"] == "loaded" and first["br"]["patterns"] == 1
    again = agencies.refresh(settings.database_url, Client(), [BRAMPTON])
    assert again["br"]["status"] == "unchanged"

    # One bus between the first two stops heading east, one already reported an hour ago.
    live = vehicle_feed(
        NOW, [{"id": "1051", "route_id": "1", "lat": 43.6860, "lon": -79.7535, "bearing": 78}]
    )
    with connect(settings.database_url) as conn:
        conn.execute(
            """INSERT INTO normalized.agency_vehicles (agency, vehicle_id, lat, lon, reported_at)
               VALUES ('br', 'gone', 43.7, -79.7, %s)""",
            (NOW - timedelta(hours=2),),
        )
        assert agencies.poll(conn, Client(live), BRAMPTON, NOW) == {
            "agency": "br", "status": "ok", "vehicles": 1,
        }  # fmt: skip
        ids = [
            r["vehicle_id"]
            for r in conn.execute("SELECT vehicle_id FROM normalized.agency_vehicles")
        ]
        assert ids == ["1051"]
        assert agencies.healthy(conn, NOW + timedelta(seconds=30)) == ["mi", "yr"]

        matched = web.matched_vehicles(conn, NOW)
        [bus] = matched
        assert bus["vehicle_id"] == "br:1051" and bus["route_id"] == "br:1" and bus["mode"] == "br"
        assert bus["pattern"]["pattern_id"] == "br:1" and 0.4 < bus["position"] < 0.6
        stop = web.stop(conn, "br:102", matched, NOW)
        assert stop["stop"]["agency"] == "Brampton Transit" and stop["stop"]["code"] == "9102"
        [arrival] = stop["arrivals"]
        assert arrival["short_name"] == "1" and arrival["towards"] == "Queen East"
        assert 30 < arrival["seconds"] < 90  # About a minute of timetable, less the report's age.
        assert arrival["source"] == "estimate"
        # Brampton's own prediction for the bus replaces the estimate.
        trips = surface_trip_feed(
            NOW, [{"trip_id": "t1", "route_id": "1", "vehicle": "1051", "stops": [("102", 45)]}]
        )
        assert agencies.poll_predictions(conn, Client(trips), BRAMPTON, NOW) == {
            "agency": "br", "status": "ok", "predictions": 1,
        }  # fmt: skip
        [arrival] = web.stop(conn, "br:102", matched, NOW)["arrivals"]
        assert (arrival["vehicle_id"], arrival["seconds"], arrival["source"]) == (
            "br:1051",
            45,
            "prediction",
        )
        assert web.stop(conn, "zz:102", matched, NOW) is None

    app = TestClient(create_app(Settings(settings.database_url), clock=lambda: NOW))
    surface = app.get("/api/surface").json()
    assert {"route_id": "br:1", "short_name": "1", "long_name": "Queen", "mode": "br",
            "agency": "Brampton Transit"} in surface["routes"]  # fmt: skip
    assert ["br:101", "9101", "Queen at Main", 43.685, -79.76, ["br:1"]] in surface["stops"]
    assert "CC BY 4.0" in surface["attribution"]
    vehicles = app.get("/api/vehicles").json()
    row = dict(zip(vehicles["fields"], vehicles["vehicles"][0], strict=True))
    assert row["id"] == "br:1051" and row["pattern"] == "br:1" and row["pace"] == 1.0
    assert "br:1" in app.get("/api/patterns").json()["patterns"]
    assert app.get("/api/stops/br:102").json()["arrivals"][0]["vehicle_id"] == "br:1051"
    route = app.get("/api/routes/br:1").json()
    assert route["route"]["mode"] == "br" and len(route["patterns"][0]["path"]) == 3
    assert app.get("/api/stops/br:nope").status_code == 404
    assert app.get("/api/routes/br:1;drop").status_code == 404


@pytest.mark.integration
def test_a_failed_poll_is_recorded_and_keeps_the_last_positions(settings):
    class Failing(Client):
        def fetch_bytes(self, url, host, max_bytes=None, sink=None):
            raise agencies.FetchError("HTTP 503")

    with connect(settings.database_url) as conn:
        result = agencies.poll(conn, Failing(), BRAMPTON, NOW)
        assert result["status"] == "failed"
        poll = conn.execute("SELECT status, error FROM ops.agency_polls").fetchone()
    assert poll == {"status": "failed", "error": "HTTP 503"}
