"""The Events page's agenda (transit.web.agenda): categories, major venues, ongoing city
events and the nearest station."""

from datetime import UTC, date, datetime, timedelta

from transit.web import agenda

UNION = {
    "name": "Union",
    "kind": "subway",
    "key": "union",
    "lines": ["1"],
    "lat": 43.6453,
    "lon": -79.3806,
}
EXHIBITION = {**UNION, "name": "Exhibition", "kind": "regional", "key": None, "lines": ["LW"]}
EXHIBITION.update(lat=43.6363, lon=-79.4187)
STATIONS = [UNION, EXHIBITION]


def test_categories_come_from_city_categories_or_ticketmaster_segments():
    assert agenda.city_category(["Live Performances", "Music"]) == "concerts"
    assert agenda.city_category(["Sports"]) == "sports"
    assert agenda.city_category(["Family/Children", "Live Performances", "Theatre"]) == "arts"
    assert agenda.city_category(["Farmers' Market", "Other"]) == "community"
    assert agenda.ticketed_category("Sports", "Hockey") == "sports"
    assert agenda.ticketed_category("Music", "Rock") == "concerts"
    assert agenda.ticketed_category("Arts & Theatre", "Musical") == "arts"
    assert agenda.ticketed_category("Miscellaneous", "Comedy") == "arts"
    assert agenda.ticketed_category("Family", None) == "community"


def test_major_venues_and_the_nearest_station_with_walking_time():
    assert agenda.is_major("Scotiabank Arena") and agenda.is_major("BUDWEISER STAGE")
    assert not agenda.is_major("Massey Hall") and not agenda.is_major(None)
    # Scotiabank Arena is about 220 m from Union: 3 minutes at 80 m a minute.
    arena = agenda.nearest_station(43.6435, -79.3791, STATIONS)
    assert arena == {
        "name": "Union",
        "kind": "subway",
        "key": "union",
        "lines": ["1"],
        "walk_min": 3,
    }
    # BMO Field is nearest the regional station.
    assert agenda.nearest_station(43.6332, -79.4186, STATIONS)["name"] == "Exhibition"
    # The subway (7 minutes away here) is preferred unless a regional station is over
    # 3 minutes closer.
    go_5_min = {**EXHIBITION, "name": "Regional", "lat": 43.6447, "lon": -79.3830}
    go_1_min = {**go_5_min, "lon": -79.3866}
    assert agenda.nearest_station(43.6447, -79.3870, [UNION, go_5_min])["name"] == "Union"
    assert agenda.nearest_station(43.6447, -79.3870, [UNION, go_1_min])["kind"] == "regional"
    # Nothing within a 20-minute walk (Scarborough Town Centre here).
    assert agenda.nearest_station(43.7757, -79.2578, STATIONS) is None


def test_build_marks_ongoing_city_events_and_merges_both_sources():
    days = agenda.window(datetime(2026, 10, 15, 16, tzinfo=UTC))
    assert days[0] == date(2026, 10, 15) and len(days) == agenda.DAYS
    market = {
        "event_id": "m", "name": "Farmers' Market", "categories": ["Farmers' Market"],
        "featured": False, "free": True, "website": None, "venue": "Nathan Phillips Square",
        "address": None, "lat": 43.6525, "lon": -79.3839,
    }  # fmt: skip
    concert = {
        **market,
        "event_id": "c",
        "name": "Jazz Night",
        "categories": ["Music"],
        "free": False,
    }
    first = datetime(2026, 10, 15, 14, tzinfo=UTC)
    city = [{**market, "starts_at": first + timedelta(days=i)} for i in range(5)]
    city.append({**concert, "starts_at": datetime(2026, 10, 16, 23, 30, tzinfo=UTC)})
    game = {
        "event_id": "G1", "name": "Maple Leafs vs. Bruins", "segment": "Sports", "genre": "Hockey",
        "starts_at": datetime(2026, 10, 16, 23, tzinfo=UTC), "local_date": date(2026, 10, 16),
        "time_tbd": False, "status": "onsale", "venue": "Scotiabank Arena", "address": None,
        "lat": 43.6435, "lon": -79.3791, "url": "https://www.ticketmaster.ca/event/G1",
    }  # fmt: skip
    body = agenda.build(city, [game], STATIONS, days)
    assert body["sources"] == {"ticketmaster": True}
    on_16 = [e for e in body["events"] if e["date"] == "2026-10-16"]
    # In time order: the market (10:00), the game (19:00), the concert (19:30).
    assert [e["name"] for e in on_16] == ["Farmers' Market", "Maple Leafs vs. Bruins", "Jazz Night"]
    market_row, game_row, concert_row = on_16
    assert market_row["ongoing"] is True and concert_row["ongoing"] is False
    assert game_row["major"] is True and game_row["category"] == "sports"
    assert game_row["station"]["name"] == "Union" and game_row["status"] is None
    assert concert_row["category"] == "concerts" and concert_row["free"] is False
    # A city event at midnight is all-day and listed first.
    midnight = {**concert, "event_id": "x", "name": "Light Installation",
                "starts_at": datetime(2026, 10, 16, 4, tzinfo=UTC)}  # fmt: skip
    first_row = agenda.build([midnight, *city], [], STATIONS, days)["events"]
    assert [e["all_day"] for e in first_row if e["date"] == "2026-10-16"][:2] == [True, False]
