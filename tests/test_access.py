"""Station entrances, exits and elevator outages for the map and trip plans."""

import json

from transit.web import access

# Two stations 1.1 km apart on a north-south line. 0.001 degrees of latitude is about 111 m.
NORTH = {"key": "north", "name": "North", "lat": 43.680, "lon": -79.400}
SOUTH = {"key": "south", "name": "South", "lat": 43.670, "lon": -79.400}


def entrance(osm_id, lon, lat, **props):
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {"id": osm_id, **props},
    }


ENTRANCES = [
    # South station: one entrance on each side, the west one step-free; one exit only.
    entrance("e", -79.3990, 43.6700, name="East Entrance", where="east side of Main St"),
    entrance("w", -79.4010, 43.6700, wheelchair="yes", where="west side of Main St"),
    entrance("x", -79.4000, 43.6690, exit_only=True, wheelchair="no", automatic=True),
    # North station; and one too far from either.
    entrance("n", -79.4000, 43.6810),
    entrance("far", -79.4500, 43.6700),
]


def test_entrances_join_the_nearest_station_and_are_named():
    by_station = access.station_entrances([NORTH, SOUTH], ENTRANCES)
    assert sorted(by_station) == ["north", "south"]
    south = {e["id"]: e for e in by_station["south"]}
    assert set(south) == {"e", "w", "x"}
    assert south["e"]["name"] == "East Entrance" and south["e"]["where"] == "east side of Main St"
    # Unnamed entrances are named by where they are from the station.
    assert south["w"]["name"] == "West entrance" and south["w"]["step_free"] is True
    assert south["x"]["name"] == "South entrance"
    assert south["x"]["exit_only"] and south["x"]["automatic"] and south["x"]["no_step_free"]
    assert by_station["north"][0]["name"] == "North entrance"


def test_bike_parking_near_each_station_is_counted():
    racks = [
        entrance("r1", -79.4005, 43.6702, capacity=10, covered=True),
        entrance("r2", -79.3995, 43.6698),
        entrance("r3", -79.4000, 43.6750, capacity=4),
    ]
    assert access.station_bike_parking([NORTH, SOUTH], racks) == {
        "south": {"places": 2, "spaces": 10, "covered": 1}
    }


def test_choice_respects_step_free_and_exit_only():
    south = access.station_entrances([SOUTH], ENTRANCES)["south"]
    below = [[-79.4000, 43.6600]]
    assert access.choose(south, below, step_free=False, leaving=True)["id"] == "x"
    # The exit-only stairs cannot be used to get in.
    assert access.choose(south, below, step_free=False, leaving=False)["id"] in {"e", "w"}
    assert access.choose(south, [[-79.39, 43.67]], step_free=True, leaving=True)["id"] == "w"
    assert access.choose([south[0]], below, step_free=True, leaving=True) is None
    assert access.choose(south, [], step_free=False, leaving=True) is None


def test_the_route_is_sampled_away_from_the_platform():
    # 1.1 km north, then the same back; 0.001 degrees of latitude is about 111 m.
    path = [[-79.4, 43.67], [-79.4, 43.68]]
    route = access.route_near_station(path)
    assert 40 <= access.metres(path[0], route[0]) <= 50
    assert 390 <= access.metres(path[0], route[-1]) <= 400
    backwards = access.route_near_station(path, from_end=True)
    assert 40 <= access.metres(path[-1], backwards[0]) <= 50
    # A walk too short to reach WALK_FROM_M still points at its far end.
    assert access.route_near_station([[-79.4, 43.67], [-79.4, 43.6702]]) == [[-79.4, 43.6702]]


def walk(path, metres):
    return {"mode": "walk", "network": None, "metres": metres, "path": path}


def subway(frm, to):
    return {"mode": "subway", "network": "ttc", "from_stop_id": frm, "to_stop_id": to}


STOPS = {"N1": {"key": "north", "name": "North"}, "S1": {"key": "south", "name": "South"}}


def test_trips_get_entrance_exit_and_outages():
    by_station = access.station_entrances([NORTH, SOUTH], ENTRANCES)
    # Arrive at North from the north, ride south, walk off to the east.
    trip = {
        "legs": [
            walk([[-79.4000, 43.6900], [-79.4000, 43.6815]], 950),
            subway("N1", "S1"),
            walk([[-79.4000, 43.6700], [-79.3990, 43.6700], [-79.3900, 43.6700]], 820),
        ]
    }
    outages = {"south": ["South: Elevator out of service"]}
    [annotated] = access.annotate([trip], STOPS, by_station, outages, step_free=False)
    ride = annotated["legs"][1]
    assert ride["entrance"]["id"] == "n" and ride["exit"]["id"] == "e"
    assert ride["outages"] == [{"station": "South", "alerts": ["South: Elevator out of service"]}]
    assert annotated["outages"] == ride["outages"]
    # Step-free, the only step-free exit is the west one; North has none, which shows as None.
    [step_free] = access.annotate([json.loads(json.dumps(trip))], STOPS, by_station, {}, True)
    assert step_free["legs"][1]["exit"]["id"] == "w"
    assert step_free["legs"][1]["entrance"] is None and step_free["outages"] == []


def test_a_short_transfer_gets_no_exit_and_step_free_trips_avoid_outages():
    by_station = access.station_entrances([NORTH, SOUTH], ENTRANCES)
    # A ride, a 60 m walk to a bus inside the station, and the bus.
    bus = {"mode": "bus", "network": "ttc", "from_stop_id": "B", "to_stop_id": "C"}
    transfer = {"legs": [subway("N1", "S1"), walk([[-79.4, 43.67], [-79.4, 43.6695]], 60), bus]}
    clear = {"legs": [subway("N1", "N1")]}
    trips = access.annotate([transfer, clear], STOPS, by_station, {"south": ["Out"]}, True)
    assert trips[0] is clear and trips[1] is transfer
    assert "exit" not in transfer["legs"][0] and "entrance" not in transfer["legs"][0]
    assert "outages" not in bus


def test_layer_files_reload_when_rebuilt(tmp_path):
    layers = access.Layers(str(tmp_path))
    assert layers.features("entrances.geojson") == []
    path = tmp_path / "entrances.geojson"
    path.write_text(json.dumps({"features": ENTRANCES[:1]}), encoding="utf-8")
    assert len(layers.features("entrances.geojson")) == 1
    first = layers.by_station([SOUTH], "v1")
    assert layers.by_station([SOUTH], "v1") is first
    assert access.Layers(None).features("entrances.geojson") == []


def test_outages_go_to_the_station_the_alert_names():
    stations = [
        {"key": "bloor-yonge", "name": "Bloor-Yonge", "stop_ids": ["13863", "13864"]},
        {"key": "sheppard-yonge", "name": "Sheppard-Yonge", "stop_ids": ["13859"]},
        {"key": "king", "name": "King", "stop_ids": ["14000"]},
    ]
    alerts = [
        # TTC tagged this Bloor-Yonge escalator with Sheppard-Yonge's platform too.
        {"header": "Bloor-Yonge: Escalator 3D10E out of service", "stop_ids": ["13859", "13864"]},
        # A header that names no matched station keeps every match.
        {"header": "Elevator out of service", "stop_ids": ["14000", "13859"]},
    ]
    assert access.station_outages(stations, alerts) == {
        "bloor-yonge": ["Bloor-Yonge: Escalator 3D10E out of service"],
        "sheppard-yonge": ["Elevator out of service"],
        "king": ["Elevator out of service"],
    }
