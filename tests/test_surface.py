"""Buses and streetcars: the static network, vehicle positions, matching and estimates."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.ttc_helpers import T0, static_zip, surface_trip_feed, vehicle_feed
from transit.db import connect
from transit.ttc import matching, static_gtfs, store, surface
from transit.web.app import create_app

# Halfway between Kennedy Rd (662) and Midland Ave (663) on the eastbound side.
MIDWAY_EAST = {"lat": 43.7, "lon": -79.265}


def test_patterns_carry_mean_scheduled_offsets_and_destinations():
    feed = surface.parse(static_zip())
    assert {r["route_id"]: r["mode"] for r in feed.routes} == {"10": "bus", "501": "streetcar"}
    east = next(p for p in feed.patterns if p["route_id"] == "10" and p["direction_id"] == 0)
    assert (east["towards"], east["trips"]) == ("Brimley", 2)
    assert [r[2:] for r in feed.pattern_stops if r[0] == east["pattern_id"]] == [
        ("662", 0),
        ("663", 180),
        ("664", 360),
    ]
    assert feed.stats == {"routes": 2, "stops": 8, "patterns": 3, "trips": 5}
    # Every trip is counted once in the scheduled starts, by pattern, service and hour.
    assert sum(row[3] for row in feed.starts) == feed.trips
    assert {row[0] for row in feed.starts} == {p["pattern_id"] for p in feed.patterns}
    assert surface.towards("East - 504A King towards Distillery Loop") == "Distillery Loop"


def index():
    feed = surface.parse(static_zip())
    stops = {s["stop_id"]: s for s in feed.stops}
    routes = {}
    for p in feed.patterns:
        rows = [r for r in feed.pattern_stops if r[0] == p["pattern_id"]]
        pattern = {
            "route": p["route_id"],
            "towards": p["towards"],
            "trips": p["trips"],
            "stops": [r[2] for r in rows],
            "offsets": [r[3] for r in rows],
            "xy": [matching._xy(stops[r[2]]["lat"], stops[r[2]]["lon"]) for r in rows],
        }
        pattern["order"] = {s: i for i, s in enumerate(pattern["stops"])}
        routes.setdefault(p["route_id"], []).append(pattern)
    return routes


def test_vehicles_snap_to_the_pattern_matching_their_heading():
    patterns = index()["10"]
    east, position = matching.locate(MIDWAY_EAST["lat"], MIDWAY_EAST["lon"], 90, patterns)
    assert east["towards"] == "Brimley" and position == pytest.approx(0.5, abs=0.02)
    west, _ = matching.locate(43.7003, -79.265, 270, patterns)
    assert west["towards"] == "Kennedy Station"
    # 500 m off the route: a detour or a trip to the garage, so no match.
    assert matching.locate(43.7045, -79.265, 90, patterns) is None


@pytest.mark.integration
def test_vehicle_snapshot_keeps_only_vehicles_in_service_inside_toronto(settings):
    with connect(settings.database_url) as conn:
        static_gtfs.load(conn, static_gtfs.parse(static_zip()), "test://gtfs", T0)
        feed = vehicle_feed(
            T0,
            [
                {"id": "8001", "route_id": "10", "bearing": 90, **MIDWAY_EAST},
                {"id": "8002", "lat": 43.7, "lon": -79.26},  # No route: out of service.
                {"id": "8003", "route_id": "10", "lat": 45.4, "lon": -75.7},  # Ottawa.
                {"id": "8004", "route_id": "10", "lat": 43.7, "lon": -79.25, "age": 900},
            ],
        )
        snapshot_id, _ = store.store_snapshot(conn, "vehicles", "test://", feed, T0)
        result = store.normalize_snapshot(conn, snapshot_id)
        assert (result["accepted"], result["rejected"]) == (1, 2)
        rows = conn.execute("SELECT vehicle_id, bearing FROM normalized.ttc_vehicles").fetchall()
        assert [(r["vehicle_id"], r["bearing"]) for r in rows] == [("8001", 90)]


@pytest.mark.integration
def test_a_near_empty_vehicle_snapshot_keeps_the_current_positions(settings):
    with connect(settings.database_url) as conn:
        static_gtfs.load(conn, static_gtfs.parse(static_zip()), "test://gtfs", T0)
        many = [
            {"id": str(9000 + n), "route_id": "10", "bearing": 90, **MIDWAY_EAST}
            for n in range(store.DROPOUT_MIN_VEHICLES)
        ]
        for when, vehicles in ((T0, many), (T0 + timedelta(seconds=30), many[:5])):
            snapshot_id, _ = store.store_snapshot(
                conn, "vehicles", "test://", vehicle_feed(when, vehicles), when
            )
            store.normalize_snapshot(conn, snapshot_id)
        count = conn.execute("SELECT count(*) AS n FROM normalized.ttc_vehicles").fetchone()["n"]
        assert count == store.DROPOUT_MIN_VEHICLES


@pytest.fixture
def client(settings):
    with connect(settings.database_url) as conn:
        feed = static_gtfs.parse(static_zip())
        static_gtfs.load(conn, feed, "test://gtfs", T0)
        surface.load(conn, surface.parse(static_zip()), feed.feed_version)
        snapshot_id, _ = store.store_snapshot(
            conn,
            "vehicles",
            "test://",
            vehicle_feed(T0, [{"id": "8001", "route_id": "10", "bearing": 90, **MIDWAY_EAST}]),
            T0,
        )
        store.normalize_snapshot(conn, snapshot_id)
    matching._index.clear()
    return TestClient(create_app(settings, clock=lambda: T0 + timedelta(seconds=10)))


@pytest.mark.integration
def test_surface_endpoints_place_vehicles_and_estimate_arrivals(client):
    vehicles = client.get("/api/vehicles").json()
    assert vehicles["matched"] == 1
    row = dict(zip(vehicles["fields"], vehicles["vehicles"][0], strict=True))
    assert (row["route"], row["mode"], row["towards"]) == ("10", "bus", "Brimley")
    # What the map needs to move it: halfway between the first two stops, no recorded
    # passages yet, so the timetable's pace.
    assert row["position"] == pytest.approx(0.5, abs=0.05) and row["pace"] == 1.0
    assert row["pattern"] is not None and row["stopped"] in (True, False)
    assert row["reported"] == int((T0 - timedelta(seconds=20)).timestamp())
    patterns = client.get("/api/patterns").json()["patterns"]
    lon, lat, offset = patterns[str(row["pattern"])][0]
    assert offset == 0 and -80 < lon < -79 and 43 < lat < 44

    # Halfway between stops 0 and 1 (90 s along); Brimley is at 360 s; the report is 30 s
    # old by the clock (20 s at T0, plus 10 s).
    arrivals = client.get("/api/stops/664").json()["arrivals"]
    assert [(a["short_name"], a["towards"], a["seconds"]) for a in arrivals] == [
        ("10", "Brimley", 360 - 90 - 30)
    ]
    # The vehicle has passed Kennedy Rd and is not heading to the westbound stops.
    assert client.get("/api/stops/662").json()["arrivals"] == []
    assert client.get("/api/stops/666").json()["arrivals"] == []

    network = client.get("/api/surface").json()
    assert len(network["stops"]) == 8
    route = client.get("/api/routes/10").json()
    assert [(p["towards"], len(p["path"])) for p in route["patterns"]] == [
        ("Brimley", 3),
        ("Kennedy Station", 3),
    ]
    assert client.get("/api/stops/nope-1").status_code == 404


@pytest.mark.integration
def test_ttc_predictions_replace_estimates_at_a_stop(client, settings):
    payload = surface_trip_feed(
        T0,
        [
            # The matched bus: TTC predicts Brimley in 200 s, which replaces the estimate.
            {"trip_id": "a", "route_id": "10", "vehicle": "8001", "stops": [("664", 200)]},
            # A bus not on the map yet, and a cancelled trip.
            {"trip_id": "b", "route_id": "10", "vehicle": "8002", "stops": [("664", 600)]},
            {"trip_id": "c", "route_id": "10", "cancelled": True, "stops": [("664", 300)]},
            # A route that does not serve the stop, and a time past the 90 minute horizon.
            {"trip_id": "d", "route_id": "99", "stops": [("664", 100)]},
            {"trip_id": "e", "route_id": "10", "stops": [("664", 6000)]},
        ],
    )
    with connect(settings.database_url) as conn:
        snapshot_id, _ = store.store_snapshot(conn, "trips_surface", "test://", payload, T0)
        assert (
            store.normalize_snapshot(conn, snapshot_id)["accepted"] == 3
        )  # a, b and d; c is cancelled, e too far ahead
    arrivals = client.get("/api/stops/664").json()["arrivals"]
    assert [(a["vehicle_id"], a["towards"], a["seconds"], a["source"]) for a in arrivals] == [
        ("8001", "Brimley", 190, "prediction"),
        ("8002", "Brimley", 590, "prediction"),
    ]


def test_pace_meets_ttc_prediction_for_a_stop_far_enough_ahead():
    from transit.web.surface import predicted_pace

    pattern = {"order": {"a": 0, "b": 1, "c": 2, "d": 3}, "offsets": [0, 30, 120, 240]}
    v = {"pattern": pattern, "position": 0.5, "reported_at": T0}  # 15 timetable s along.
    # b is behind 60 s of timetable from here; c is 105 s ahead, predicted 70 s after the
    # report: the vehicle is running 1.5x the timetable.
    stops = [("b", T0 + timedelta(seconds=10)), ("c", T0 + timedelta(seconds=70))]
    assert predicted_pace(v, stops) == pytest.approx(105 / 70)
    # Stops behind it, not on its pattern, or nothing far enough ahead: no prediction.
    assert predicted_pace(v, [("a", T0), ("x", T0 + timedelta(seconds=90))]) is None
    assert predicted_pace(v, [("b", T0 + timedelta(seconds=10))]) is None
