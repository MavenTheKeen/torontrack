"""Notable places to eat from Wikidata: reading the SPARQL answer, loading and matching."""

import json
from datetime import UTC, datetime

import pytest

from transit import food_notable
from transit.db import connect

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)


def binding(qid, name, lon, lat, opened=None, wikipedia=None, awards=None) -> dict:
    row = {
        "place": {"value": f"http://www.wikidata.org/entity/{qid}"},
        "name": {"value": name},
        "coord": {"value": f"Point({lon} {lat})"},
    }
    if opened:
        row["opened"] = {"value": opened}
    if wikipedia:
        row["wikipedia"] = {"value": wikipedia}
    if awards is not None:
        row["awards"] = {"value": awards}
    return row


ANSWER = {
    "results": {
        "bindings": [
            binding("Q1", "Sneaky Dee's", -79.4066, 43.6560, "1987-01-01T00:00:00Z",
                    "https://en.wikipedia.org/wiki/Sneaky_Dee%27s", ""),
            binding("Q2", "Enigma Yorkville", -79.3906, 43.6689, None, None, "Michelin star"),
            binding("Q3", "Some Cafe", -79.40, 43.65, "1990-01-01T00:00:00Z", None, ""),
            binding("Q4", "Bad Point", -79.40, 43.65, None, "https://en.wikipedia.org/wiki/X"),
        ]
    }
}  # fmt: skip
ANSWER["results"]["bindings"][3]["coord"]["value"] = "Point(nowhere)"


def test_only_places_with_an_article_or_a_michelin_award_are_kept():
    assert food_notable.read(ANSWER) == [
        ("Q1", "Sneaky Dee's", 43.656, -79.4066, 1987,
         "https://en.wikipedia.org/wiki/Sneaky_Dee%27s", None),
        ("Q2", "Enigma Yorkville", 43.6689, -79.3906, None, None, "Michelin star"),
    ]  # fmt: skip


def test_a_notable_place_matches_the_nearest_place_with_its_name():
    places = [
        [-79.4066, 43.6575, "Sneaky Dee's", 0],  # about 170 m north: too far
        [-79.4067, 43.6561, "Sneaky Dees", 0],  # about 13 m away
        [-79.4066, 43.6560, "Pizza Place", 2],  # same spot, another name
        [-79.3907, 43.6690, "Enigma", 0],
    ]
    notable = [
        {"name": name, "lat": lat, "lon": lon}
        for _, name, lat, lon, *_ in food_notable.read(ANSWER)
    ]
    found = food_notable.match(places, notable)
    assert {index: n["name"] for index, n in found.items()} == {
        1: "Sneaky Dee's",
        3: "Enigma Yorkville",
    }


class FakeClient:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.headers = []

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None, headers=None):
        assert host == "query.wikidata.org" and url.startswith(f"https://{host}/sparql?")
        self.headers.append(headers)
        return json.dumps(self.bodies.pop(0)).encode()


@pytest.mark.integration
def test_refresh_replaces_the_list_and_a_short_answer_is_not_loaded(settings, monkeypatch):
    monkeypatch.setattr(food_notable, "MIN_PLACES", 2)
    url = settings.database_url
    client = FakeClient([ANSWER, {"results": {"bindings": ANSWER["results"]["bindings"][:1]}}])
    assert food_notable.refresh(url, client, NOW) == {"status": "loaded", "places": 2}
    assert "torontrack" in client.headers[0]["User-Agent"].lower()
    assert food_notable.refresh(url, client, NOW)["status"] == "failed"
    with connect(url) as conn:
        assert [r["name"] for r in food_notable.listed(conn)] == [
            "Sneaky Dee's",
            "Enigma Yorkville",
        ]
