"""Lines 5 and 6: light rail in TTC's GTFS (route_type 0, as streetcars) loaded with the
subway, and placed by timetable because TTC publishes no realtime data for them."""

import dataclasses
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.ttc_helpers import LINE_6_EAST, LINE_6_WEST, T0, static_zip
from transit.db import connect
from transit.ttc import static_gtfs, surface
from transit.ttc.sources import LIGHT_RAIL_LINES
from transit.web.app import create_app

LINE_6_STOPS = {s for s, *_ in LINE_6_EAST + LINE_6_WEST}


def subway_only(feed):
    """The feed as it was loaded before light rail was included."""
    trips = {t["trip_id"] for t in feed.trips if t["route_id"] not in LIGHT_RAIL_LINES}
    return dataclasses.replace(
        feed,
        routes=[r for r in feed.routes if r["route_id"] not in LIGHT_RAIL_LINES],
        stops=[s for s in feed.stops if s["stop_id"] not in LINE_6_STOPS],
        line_stops=[r for r in feed.line_stops if r["route_id"] not in LIGHT_RAIL_LINES],
        trips=[t for t in feed.trips if t["trip_id"] in trips],
        stop_times=[r for r in feed.stop_times if r[0] in trips],
    )


def test_light_rail_lines_are_loaded_with_the_subway_and_join_its_stations():
    feed = static_gtfs.parse(static_zip(light_rail=True))
    assert [r["route_id"] for r in feed.routes] == ["1", "2", "6"]
    east = [r for r in feed.line_stops if (r["route_id"], r["direction_id"]) == ("6", 0)]
    assert [r["stop_id"] for r in east] == [s for s, *_ in LINE_6_EAST]
    assert {r["towards"] for r in east} == {"Martin Grove"}
    names = {s["stop_id"]: (s["station_name"], s["platform"]) for s in feed.stops}
    assert names["16289"] == ("Humber College", "LRT")
    assert names["16292"] == ("Westmore", "Westbound")
    keys = {s["station_key"] for s in feed.stops if s["stop_id"] in ("16289", "16290")}
    assert keys == {"humber-college"}


def test_the_surface_network_leaves_out_light_rail_but_keeps_streetcars():
    full = surface.parse(static_zip(light_rail=True))
    network = surface.without_routes(full, LIGHT_RAIL_LINES)
    assert {r["route_id"] for r in network.routes} == {"10", "501"}
    assert not {s["stop_id"] for s in network.stops} & LINE_6_STOPS
    kept = {p["pattern_id"] for p in network.patterns}
    assert kept == {p["pattern_id"] for p in full.patterns if p["route_id"] != "6"}
    assert all(row[0] in kept for row in network.pattern_stops + network.starts)
    assert network.stats["routes"] == 2


@pytest.mark.integration
def test_a_version_loaded_before_light_rail_gains_it(settings):
    payload = static_zip(light_rail=True)
    feed = static_gtfs.parse(payload)
    with connect(settings.database_url) as conn:
        assert static_gtfs.load(conn, subway_only(feed), "test://gtfs", T0)
        surface.load(conn, surface.parse(payload), feed.feed_version)
        assert static_gtfs.load(conn, feed, "test://gtfs", T0) is False
        assert static_gtfs.add_lines(conn, feed) == ["6"]
        assert static_gtfs.add_lines(conn, feed) == []
        assert surface.remove_routes(conn, feed.feed_version, LIGHT_RAIL_LINES) == ["6"]
        assert surface.remove_routes(conn, feed.feed_version, LIGHT_RAIL_LINES) == []
        counts = conn.execute(
            """SELECT
                 (SELECT count(*) FROM normalized.ttc_line_stops
                  WHERE feed_version = %(v)s AND route_id = '6') AS line_stops,
                 (SELECT count(*) FROM normalized.ttc_stop_times st
                  JOIN normalized.ttc_trips t USING (feed_version, trip_id)
                  WHERE st.feed_version = %(v)s AND t.route_id = '6') AS stop_times,
                 (SELECT count(*) FROM normalized.ttc_surface_stops
                  WHERE feed_version = %(v)s AND stop_id = ANY(%(s)s)) AS surface_stops,
                 (SELECT count(*) FROM normalized.ttc_surface_routes
                  WHERE feed_version = %(v)s) AS surface_routes""",
            {"v": feed.feed_version, "s": sorted(LINE_6_STOPS)},
        ).fetchone()
        assert counts == {
            "line_stops": 6,
            "stop_times": 12,
            "surface_stops": 0,
            "surface_routes": 2,
        }


@pytest.fixture
def client(settings):
    with connect(settings.database_url) as conn:
        static_gtfs.load(conn, static_gtfs.parse(static_zip(light_rail=True)), "test://gtfs", T0)
    return TestClient(create_app(settings, clock=lambda: T0 + timedelta(seconds=10)))


@pytest.mark.integration
def test_light_rail_trains_are_placed_by_timetable(client):
    lines = {line["id"]: line for line in client.get("/api/lines").json()["lines"]}
    assert lines["6"]["timetable"] is True and lines["1"]["timetable"] is False
    body = client.get("/api/lines/6").json()
    assert [s["name"] for s in body["stations"]] == ["Humber College", "Westmore", "Martin Grove"]
    trains = {t["train_id"]: t for t in body["trains"]}
    # It is 09:00:10. Eastbound 600 left Humber College at 08:56 and reaches Martin Grove
    # at 09:02, 110 s away on a 180 s run from Westmore; 601 leaves at 09:06, so it is not
    # running yet. Westbound 610 left Martin Grove at 08:58 and reaches Westmore at 09:01.
    assert set(trains) == {"600", "610"}
    east = trains["600"]
    assert east["scheduled"] is True and east["at_station"] is False
    assert east["position"] == pytest.approx(2 - 110 / 180, abs=0.001)
    assert east["next_station"] == "Martin Grove"
    west = trains["610"]
    assert west["direction_id"] == 1
    assert west["position"] == pytest.approx(1 + 50 / 180, abs=0.001)
    every = client.get("/api/trains").json()
    assert {
        t["train_id"] for line in every["lines"] if line["id"] == "6" for t in line["trains"]
    } == {
        "600",
        "610",
    }


@pytest.mark.integration
def test_light_rail_stations_show_scheduled_arrivals(client):
    station = client.get("/api/stations/humber-college").json()["station"]
    assert station["lines"] == ["6"]
    by_stop = {d["stop_id"]: d for d in station["directions"]}
    departures = by_stop["16289"]["arrivals"]
    assert [(a["train_id"], a["minutes"], a["scheduled"]) for a in departures] == [
        ("601", 5, True),
        ("602", 15, True),
    ]
    assert [a["train_id"] for a in by_stop["16290"]["arrivals"]] == ["610"]
