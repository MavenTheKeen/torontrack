"""City of Toronto drop-in skating and swimming: reading, loading and today's sessions."""

import json
from datetime import UTC, date, datetime, time

import pytest

from transit import recreation
from transit.db import connect

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
FACILITIES = [
    {"LOCATIONID": "10", "ASSET_NAME": "WEST RINK PARK", "TYPE": "Park",
     "ADDRESS": "1 West St", "URL": "https://www.toronto.ca/x?id=10",
     "geometry": json.dumps({"type": "Point", "coordinates": [-79.43, 43.66]})},
    {"LOCATIONID": "11", "ASSET_NAME": "NO POINT", "TYPE": "Park", "ADDRESS": "None",
     "URL": "None", "geometry": json.dumps({"type": "Polygon", "coordinates": []})},
]  # fmt: skip


def session(location, section, title, day="2026-11-12", start=(18, 0), end=(19, 30), **extra):
    return {
        "Location ID": location, "Section": section, "Course Title": title,
        "First Date": day, "Last Date": day, "Start Hour": start[0], "Start Minute": start[1],
        "End Hour": end[0], "End Min": end[1], "Age Min": "6", "Age Max": "None", **extra,
    }  # fmt: skip


SESSIONS = [
    session(10, "Skate - Drop-In", "Leisure Skate"),
    session(10, "Swim - Drop-In", "Lane Swim", start=(7, 0), end=(8, 0)),
    session(10, "Skate - Drop-In", "Shinny: Adult", day="2026-11-13"),
    session(10, "FitnessTO - Drop-In", "Yoga"),
    session(12, "Skate - Drop-In", "Leisure Skate", start=("x", 0)),
]


def test_facilities_with_points_and_skating_and_swimming_sessions_are_read():
    assert recreation.read_facilities(FACILITIES) == [
        (10, "West Rink Park", "Park", "1 West St", "https://www.toronto.ca/x?id=10",
         -79.43, 43.66),
    ]  # fmt: skip
    assert recreation.read_sessions(SESSIONS) == [
        (10, "skate", "Leisure Skate", date(2026, 11, 12), time(18), time(19, 30), 6, None),
        (10, "swim", "Lane Swim", date(2026, 11, 12), time(7), time(8), 6, None),
        (10, "skate", "Shinny: Adult", date(2026, 11, 13), time(18), time(19, 30), 6, None),
    ]


class FakeClient:
    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None, headers=None):
        if url.endswith(f"id={recreation.FACILITIES_PACKAGE}"):
            body = {"result": {"resources": [{"id": "fac", "datastore_active": True}]}}
        elif url.endswith(f"id={recreation.PROGRAMS_PACKAGE}"):
            body = {"result": {"resources": [
                {"id": "loc", "name": "Locations", "datastore_active": True},
                {"id": "drop", "name": "Drop-in", "datastore_active": True},
            ]}}  # fmt: skip
        else:
            rows = FACILITIES if "/dump/fac" in url else SESSIONS
            assert "/dump/fac" in url or "/dump/drop" in url, url
            fields = list(rows[0])
            body = {"fields": [{"id": f} for f in fields],
                    "records": [[r.get(f) for f in fields] for r in rows]}  # fmt: skip
        return json.dumps(body).encode()


@pytest.mark.integration
def test_refresh_and_todays_sessions(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    monkeypatch.setattr(recreation, "MIN_FACILITIES", 1)
    monkeypatch.setattr(recreation, "MIN_SESSIONS", 1)
    url = settings.database_url
    assert recreation.refresh(url, FakeClient(), NOW) == {
        "status": "loaded",
        "facilities": 1,
        "sessions": 3,
    }
    with connect(url) as conn:
        [place] = recreation.day_sessions(conn, date(2026, 11, 12))
    assert place["name"] == "West Rink Park"
    assert [(s["kind"], s["starts"], s["ends"]) for s in place["sessions"]] == [
        ("swim", "07:00", "08:00"),
        ("skate", "18:00", "19:30"),
    ]
    client = TestClient(create_app(Settings(url), clock=lambda: NOW))
    body = client.get("/api/recreation").json()
    assert body["day"] == "2026-11-12" and body["places"][0]["id"] == 10
