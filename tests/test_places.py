"""Address, place and intersection search for the trip planner."""

from fastapi.testclient import TestClient

from transit import place_index, places
from transit.config import Settings
from transit.web.app import create_app

# Two streets crossing, a few addresses, a museum and a shop with a similar name, and two
# neighbourhood nodes. 0.001 degrees is about 111 m north-south and 80 m east-west.
NODES = [
    (1, -79.400, 43.660, {}),
    (2, -79.390, 43.660, {}),
    (3, -79.395, 43.655, {}),
    (4, -79.395, 43.665, {}),
    (5, -79.395, 43.660, {}),  # Where the two streets cross.
    (10, -79.3940, 43.6605, {"addr:housenumber": "100", "addr:street": "Queen Street West"}),
    (11, -79.3941, 43.6606, {"addr:housenumber": "100", "addr:street": "Queen Street West"}),
    (
        12,
        -79.6000,
        43.7000,
        {"addr:housenumber": "100", "addr:street": "Queen Street West", "addr:city": "Brampton"},
    ),
    (13, -79.3930, 43.6595, {"addr:housenumber": "1000", "addr:street": "Queen Street West"}),
    (20, -79.3945, 43.6670, {"name": "Royal Ontario Museum", "tourism": "museum"}),
    (21, -79.3900, 43.6500, {"name": "Royal Pizza", "amenity": "restaurant"}),
    (22, -79.3901, 43.6501, {"name": "Queen Street West Stop", "public_transport": "platform"}),
    (30, -79.3950, 43.6620, {"name": "Queen West", "place": "neighbourhood"}),
    (31, -79.6000, 43.7010, {"name": "Downtown Brampton", "place": "suburb"}),
]
WAYS = [
    (100, [1, 5, 2], {"highway": "secondary", "name": "Queen Street West"}),
    (101, [3, 5, 4], {"highway": "primary", "name": "Spadina Avenue"}),
]


def osm_file(path):
    def tags(t):
        return "".join(f'<tag k="{k}" v="{v}"/>' for k, v in t.items())

    body = [
        f'<node id="{i}" version="1" lat="{lat}" lon="{lon}">{tags(t)}</node>'
        for i, lon, lat, t in NODES
    ]
    for i, refs, t in WAYS:
        nds = "".join(f'<nd ref="{n}"/>' for n in refs)
        body.append(f'<way id="{i}" version="1">{nds}{tags(t)}</way>')
    path.write_text(f'<?xml version="1.0"?><osm version="0.6">{"".join(body)}</osm>')
    return path


def index(tmp_path):
    result = place_index.build_index(osm_file(tmp_path / "t.osm"), tmp_path)
    return tmp_path / result["file"], result


def test_text_is_normalized_the_same_way_for_the_index_and_queries():
    assert places.normalize("100 Queen Street West") == ["100", "queen", "st", "w"]
    assert places.normalize("St. Clair Ave. E") == ["st", "clair", "ave", "e"]
    assert places.query_tokens("Queen and Spadina") == ["queen", "spadina"]


def test_index_holds_addresses_places_and_intersections(tmp_path):
    path, result = index(tmp_path)
    # The two Toronto copies of 100 Queen St W are one address; transit stops are left out.
    assert result["counts"] == {"address": 3, "place": 2, "intersection": 1}
    [crossing] = places.search(path, "queen & spadina")
    assert crossing["name"] == "Queen St W & Spadina Ave" and crossing["kind"] == "intersection"
    assert crossing["detail"] == "Queen West"
    assert (crossing["lon"], crossing["lat"]) == (-79.395, 43.66)


def test_search_ranks_by_what_the_query_looks_like_and_by_distance(tmp_path):
    path, _ = index(tmp_path)
    downtown = (-79.395, 43.66)
    first, second, third = places.search(path, "100 queen st w", near=downtown)
    assert (first["name"], first["detail"]) == ("100 Queen St W", "Queen West")
    assert (second["name"], second["detail"]) == ("100 Queen St W", "Brampton")
    assert third["name"] == "1000 Queen St W"
    # Near Brampton, its 100 Queen St W comes first.
    assert places.search(path, "100 queen", near=(-79.6, 43.7))[0]["detail"] == "Brampton"
    # A name: a museum before a restaurant sharing the word; a query word in the
    # neighbourhood name alone does not beat one in the place's own name.
    assert [p["name"] for p in places.search(path, "royal")] == [
        "Royal Ontario Museum",
        "Royal Pizza",
    ]
    assert places.search(path, "queen west")[0]["kind"] != "intersection"
    assert places.search(path, "") == [] and places.search(tmp_path / "none.sqlite", "queen") == []


def test_places_endpoint(tmp_path):
    index(tmp_path)
    web = TestClient(create_app(Settings("postgresql://unused/db", map_dir=str(tmp_path))))
    body = web.get("/api/places", params={"q": "royal ontario", "near": "43.66,-79.39"}).json()
    assert body["available"] and body["places"][0]["name"] == "Royal Ontario Museum"
    assert web.get("/api/places", params={"q": "x"}).status_code == 422
    assert web.get("/api/places", params={"q": "royal", "near": "here"}).status_code == 422
    missing = Settings("postgresql://unused/db", map_dir=str(tmp_path / "missing"))
    empty = TestClient(create_app(missing))
    assert empty.get("/api/places", params={"q": "royal"}).json() == {
        "available": False,
        "places": [],
    }
