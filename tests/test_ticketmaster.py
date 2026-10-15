"""Ticketmaster events for the Events page (transit.ticketmaster).

The responses here are built by hand rather than recorded: Ticketmaster's terms allow
storing event content only for reasonable periods to provide the service, which rules out
keeping real responses in a public repository. Their structure (fields, nesting, string
coordinates, paging) was checked against a real response on 2026-10-15, and the cases
below are the ones that response and the first full load showed.
"""

import json
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from transit import ticketmaster
from transit.db import connect


def tm_event(event_id, name, *, when="2026-10-17T23:00:00Z", local_date="2026-10-17",
             venue="Scotiabank Arena", venue_id="KovZpZAEkn6A", lat="43.6435", lon="-79.3791",
             segment="Sports", genre="Basketball", status="onsale", **start):  # fmt: skip
    return {
        "name": name,
        "type": "event",
        "id": event_id,
        "url": f"https://www.ticketmaster.ca/event/{event_id}",
        "dates": {
            "start": {"localDate": local_date, "localTime": "19:00:00", "dateTime": when, **start},
            "timezone": "America/Toronto",
            "status": {"code": status},
        },
        "classifications": [
            {"primary": True, "segment": {"name": segment}, "genre": {"name": genre}}
        ],
        "_embedded": {
            "venues": [
                {
                    "name": venue,
                    "id": venue_id,
                    "address": {"line1": "40 Bay St."},
                    "location": {"longitude": lon, "latitude": lat},
                }
            ]
        },
    }


def page(events, number=0, total_pages=1):
    return json.dumps(
        {
            "_embedded": {"events": events},
            "page": {
                "size": 200,
                "totalElements": len(events),
                "totalPages": total_pages,
                "number": number,
            },  # fmt: skip
        }
    ).encode()


GAME = tm_event("G1", "Toronto Raptors vs. Chicago Bulls")


def test_events_are_parsed_and_add_ons_and_cancellations_skipped():
    row, why = ticketmaster.parse_event(GAME)
    assert why is None
    assert row["name"] == "Toronto Raptors vs. Chicago Bulls"
    assert row["starts_at"] == datetime(2026, 10, 17, 23, tzinfo=UTC)
    assert row["local_date"] == date(2026, 10, 17) and row["time_tbd"] is False
    assert (row["segment"], row["venue"], row["lat"]) == ("Sports", "Scotiabank Arena", 43.6435)
    assert row["url"] == "https://www.ticketmaster.ca/event/G1"
    skipped = [
        tm_event("P1", "Raptors vs. Bulls - Parking"),
        tm_event("S1", "Suite Rental: Raptors vs Bulls"),
        tm_event("C1", "Cancelled Show", status="cancelled"),
        tm_event("F1", "Faraway Show", lat="45.42", lon="-75.69"),  # Ottawa.
    ]
    assert [ticketmaster.parse_event(e)[1] for e in skipped] == [
        "add_on", "add_on", "cancelled", "outside_area",
    ]  # fmt: skip
    tba, _ = ticketmaster.parse_event(tm_event("T1", "Show", when=None, timeTBA=True))
    assert tba["time_tbd"] is True and tba["starts_at"] is None


def test_listings_that_are_not_events_on_the_day_are_skipped():
    day = date(2026, 10, 17)
    not_events = [
        tm_event("A1", "Leafs Fan Access Plus Ups: Fanboni", segment="Miscellaneous"),
        tm_event("A2", "Guided Tours of Scotiabank Arena", segment="Miscellaneous"),
        tm_event("A3", "TMU Bold Women's Hockey - 2026-27 Season Pass"),
        {**tm_event("A4", "Test Event"), "test": True},
        # Still running from August: a day's query returns it too.
        tm_event("A5", "Koto Fall Introductory Class", local_date="2026-08-23"),
    ]
    assert [ticketmaster.parse_event(e, day)[1] for e in not_events] == [
        "add_on", "add_on", "add_on", "test_listing", "started_earlier",
    ]  # fmt: skip
    # 23:59 is a placeholder time, not a start.
    late, _ = ticketmaster.parse_event(tm_event("L1", "Tour", localTime="23:59:00"), day)
    assert late["time_tbd"] is True and late["starts_at"] is None


def test_one_game_listed_several_times_is_kept_once():
    rows = [
        ticketmaster.parse_event(e)[0]
        for e in (GAME, tm_event("G2", "Toronto Raptors vs. Chicago Bulls - Premium Seating"))
    ]
    skipped = {}
    merged = ticketmaster.merge(rows, skipped)
    assert [r["event_id"] for r in merged] == ["G1"] and skipped == {"duplicate_listing": 1}


def test_a_timed_entry_attraction_is_one_row_per_day_with_its_sessions():
    slots = [
        tm_event(
            f"H{i}",
            "Legends of Horror",
            when=f"2026-10-17T23:{i * 5:02d}:00Z",
            venue="Casa Loma",
            venue_id="casa",
        )  # fmt: skip
        for i in range(8)
    ]
    skipped = {}
    merged = ticketmaster.merge([ticketmaster.parse_event(e)[0] for e in reversed(slots)], skipped)
    assert len(merged) == 1 and merged[0]["event_id"] == "H0" and merged[0]["sessions"] == 8
    assert skipped == {"extra_session": 7}


def test_each_day_is_requested_from_toronto_midnight_and_pages_followed():
    url = ticketmaster.request_url("k3y", date(2026, 10, 17), 0)
    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).hostname == "app.ticketmaster.com"
    assert query["startDateTime"] == ["2026-10-17T04:00:00Z"]  # EDT
    assert query["endDateTime"] == ["2026-10-18T04:00:00Z"]
    winter = parse_qs(urlsplit(ticketmaster.request_url("k", date(2026, 12, 1), 0)).query)
    assert winter["startDateTime"] == ["2026-12-01T05:00:00Z"]  # EST

    class Client:
        def __init__(self):
            self.urls = []

        def fetch_bytes(self, url, host):
            assert host == "app.ticketmaster.com"
            self.urls.append(url)
            number = int(parse_qs(urlsplit(url).query)["page"][0])
            if "2026-10-17T" in url:  # Two pages on the first day, none after.
                return page([GAME] if number == 0 else [tm_event("X", "Raptors vs. Knicks",
                            when="2026-10-17T23:30:00Z", venue_id="other")], number, 2)  # fmt: skip
            return page([], number, 0)

    client = Client()
    rows, skipped, requests = ticketmaster.read(
        client, "k", date(2026, 10, 17), sleep=lambda s: None
    )
    assert requests == ticketmaster.DAYS + 1
    assert [r["event_id"] for r in rows] == ["G1", "X"]


@pytest.mark.integration
def test_refresh_without_a_key_is_skipped_and_with_one_replaces_upcoming(settings, monkeypatch):
    monkeypatch.delenv("TICKETMASTER_API_KEY", raising=False)
    now = datetime(2026, 10, 17, 14, tzinfo=UTC)
    assert ticketmaster.refresh(settings.database_url, None, now)["status"] == "skipped"

    class Client:
        def fetch_bytes(self, url, host):
            return page([GAME]) if "2026-10-17T" in url else page([])

    monkeypatch.setenv("TICKETMASTER_API_KEY", "secret-key")
    with connect(settings.database_url) as conn:
        conn.execute(
            """INSERT INTO normalized.ticketed_events (event_id, name, local_date, time_tbd,
                   status, lat, lon, loaded_at)
               VALUES ('old', 'Long past', '2026-10-01', true, 'onsale', 43.6, -79.4, now()),
                      ('gone', 'Dropped from the listing', '2026-10-20', true, 'onsale',
                       43.6, -79.4, now())"""
        )
    result = ticketmaster.refresh(settings.database_url, Client(), now, sleep=lambda s: None)
    assert result["status"] == "loaded" and result["events"] == 1
    with connect(settings.database_url) as conn:
        ids = [
            r["event_id"] for r in conn.execute("SELECT event_id FROM normalized.ticketed_events")
        ]
        statuses = [r["status"] for r in conn.execute(
            "SELECT status FROM ops.ticketed_event_refreshes ORDER BY refresh_id")]  # fmt: skip
    assert ids == ["G1"] and statuses == ["skipped", "loaded"]

    class Failing:
        def fetch_bytes(self, url, host):
            raise ticketmaster.FetchError(f"HTTP 401 for {url}")

    failed = ticketmaster.refresh(settings.database_url, Failing(), now, sleep=lambda s: None)
    assert failed["status"] == "failed" and "secret-key" not in failed["error"]
