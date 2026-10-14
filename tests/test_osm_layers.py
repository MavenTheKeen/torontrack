"""Station entrances, parking and bike parking from OpenStreetMap."""

import json

from transit import osm_layers

# A small map around one corner: Bloor Street West runs east-west along latitude 43.667 and
# Spadina Road north-south along longitude -79.404. 0.0001 degrees of latitude is about 11 m;
# of longitude, about 8 m.
NODES = {
    1: (-79.410, 43.667),
    2: (-79.400, 43.667),
    3: (-79.404, 43.664),
    4: (-79.404, 43.670),
    # A garage south of Bloor.
    10: (-79.4030, 43.6662),
    11: (-79.4025, 43.6662),
    12: (-79.4025, 43.6667),
    13: (-79.4030, 43.6667),
    # A customers-only lot and an unnamed, untagged lot.
    20: (-79.4080, 43.6680),
    21: (-79.4075, 43.6680),
    22: (-79.4075, 43.6685),
    30: (-79.4090, 43.6650),
    31: (-79.4085, 43.6650),
    32: (-79.4085, 43.6655),
}
WAYS = [
    (100, [1, 2], {"highway": "secondary", "name": "Bloor Street West"}),
    (101, [3, 4], {"highway": "secondary", "name": "Spadina Road"}),
    (
        102,
        [10, 11, 12, 13, 10],
        {
            "amenity": "parking",
            "parking": "multi-storey",
            "fee": "yes",
            "operator": "Toronto Parking Authority",
            "capacity": "300",
        },
    ),
    (103, [20, 21, 22, 20], {"amenity": "parking", "access": "customers", "name": "Plaza"}),
    (104, [30, 31, 32, 30], {"amenity": "parking"}),
]
POINTS = [
    (
        200,
        (-79.4045, 43.6672),
        {
            "railway": "subway_entrance",
            "name": "Spadina Road West Entrance",
            "wheelchair": "yes",
        },
    ),
    (
        201,
        (-79.4020, 43.6670),
        {"railway": "subway_entrance", "automated": "yes", "entrance": "exit"},
    ),
    (202, (-79.4027, 43.6667), {"amenity": "parking_entrance", "parking": "multi-storey"}),
    (
        203,
        (-79.3900, 43.6500),
        {"amenity": "parking_entrance", "parking": "underground", "fee": "yes"},
    ),
    (204, (-79.4027, 43.6668), {"amenity": "parking_entrance", "access": "private"}),
    (205, (-79.4060, 43.6668), {"amenity": "bicycle_parking", "capacity": "10", "covered": "yes"}),
    (206, (-79.4061, 43.6668), {"amenity": "bicycle_parking", "access": "private"}),
]


def osm_file(path):
    def tags(t):
        return "".join(f'<tag k="{k}" v="{v}"/>' for k, v in t.items())

    nodes = [(i, coord, {}) for i, coord in NODES.items()] + POINTS
    body = [
        f'<node id="{i}" version="1" lat="{lat}" lon="{lon}">{tags(t)}</node>'
        for i, (lon, lat), t in nodes
    ]
    for i, refs, t in WAYS:
        nds = "".join(f'<nd ref="{n}"/>' for n in refs)
        body.append(f'<way id="{i}" version="1">{nds}{tags(t)}</way>')
    path.write_text(
        f'<?xml version="1.0"?><osm version="0.6">{"".join(body)}</osm>', encoding="utf-8"
    )
    return path


def test_places_get_their_street_and_side():
    assert osm_layers.short_street("Bloor Street West") == "Bloor St W"
    assert osm_layers.short_street("Spadina Road") == "Spadina Rd"
    assert osm_layers.short_street("The Esplanade") == "The Esplanade"
    bloor = ((-79.41, 43.667), (-79.40, 43.667))
    distance, side = osm_layers.street_side((-79.405, 43.6672), *bloor)
    assert 20 < distance < 25 and side == "north"
    assert osm_layers.street_side((-79.405, 43.6668), *bloor)[1] == "south"
    assert osm_layers.street_side((-79.405, 43.667), *bloor)[1] is None
    # Past the end of a street, the side is still across it, not "east of its end".
    assert osm_layers.street_side((-79.3999, 43.6671), *bloor)[1] == "north"


def test_layers_keep_public_places_with_their_street(tmp_path):
    result = osm_layers.build(str(tmp_path / "out"), source=str(osm_file(tmp_path / "t.osm")))
    out = tmp_path / "out"
    assert set(result["files"]) == {
        "entrances.geojson",
        "parking.geojson",
        "bike-parking.geojson",
        "places.sqlite",
    }
    assert result["counts"]["search"] == {}  # Nothing here to search; see test_places.py.
    assert json.loads((out / "osm-layers.json").read_text())["counts"] == result["counts"]

    def props(name):
        return [f["properties"] for f in json.loads((out / name).read_text())["features"]]

    named, unnamed = props("entrances.geojson")
    assert named["name"] == "Spadina Road West Entrance" and named["wheelchair"] == "yes"
    assert named["where"] == "north side of Bloor St W"
    assert unnamed["where"] == "on Bloor St W" and "side" not in unnamed
    assert unnamed["automatic"] is True and unnamed["exit_only"] is True

    parking = props("parking.geojson")
    lots = {p["id"]: p for p in parking if p["role"] == "lot"}
    # Customer-only and untagged lots are left out; an entrance with no lot becomes one.
    assert set(lots) == {"w102", "n203"}
    garage = lots["w102"]
    assert garage["kind"] == "multi-storey" and garage["fee"] is True
    assert garage["capacity"] == 300 and garage["entrances"] == 1
    assert garage["where"] == "near Bloor St W" and "side" not in garage
    [door] = [p for p in parking if p["role"] == "entrance" and p["lot"] == "w102"]
    assert door["where"] == "south side of Bloor St W"
    assert lots["n203"]["kind"] == "underground" and "where" not in lots["n203"]
    assert result["counts"]["entrances_without_car_park"] == 1

    [rack] = props("bike-parking.geojson")
    assert rack["capacity"] == 10 and rack["covered"] is True
    assert result["counts"]["bike_parking_spaces"] == 10
