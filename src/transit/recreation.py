"""City of Toronto drop-in skating and swimming, for the map's city layers.

Two city datasets are combined. Parks and recreation facilities gives each location's
name, address and position. Registered programs and drop-in courses lists every drop-in
session for the coming weeks, one row per date, with its location, title and times; the
skating sections (leisure skates and shinny) and swimming sections (leisure, lane and
other swims) are kept. Both are replaced on each load; the city refreshes them weekly.

Contains information licensed under the Open Government Licence – Toronto.
"""

import json
from datetime import UTC, date, datetime, time

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

FACILITIES_PACKAGE = "parks-and-recreation-facilities"
PROGRAMS_PACKAGE = "registered-programs-and-drop-in-courses-offering"
MAX_BYTES = 60 << 20
SECTIONS = {"Skate - Drop-In": "skate", "Swim - Drop-In": "swim"}
MIN_FACILITIES = 1000
MIN_SESSIONS = 500


def _dump(client: FeedClient, package: str, name: str | None = None) -> list[dict]:
    meta = json.loads(
        client.fetch_bytes(
            f"https://{STATIC_HOST}/api/3/action/package_show?id={package}", host=STATIC_HOST
        )
    )
    resources = [
        r
        for r in meta["result"]["resources"]
        if r.get("datastore_active") and (name is None or r.get("name") == name)
    ]
    if not resources:
        raise FetchError(f"No datastore table in {package}")
    body = json.loads(
        client.fetch_bytes(
            f"https://{STATIC_HOST}/datastore/dump/{resources[0]['id']}?format=json",
            host=STATIC_HOST,
            max_bytes=MAX_BYTES,
        )
    )
    fields = [f["id"] for f in body["fields"]]
    return [dict(zip(fields, row, strict=True)) for row in body["records"]]


def _text(value) -> str | None:
    value = str(value or "").strip()
    return None if value in ("", "None") else value


def read_facilities(rows: list[dict]) -> list[tuple]:
    """(location ID, name, type, address, URL, lon, lat) for facilities with a point."""
    found = {}
    for row in rows:
        try:
            geometry = json.loads(row["geometry"])
            lon, lat = geometry["coordinates"][:2]
            location = int(row["LOCATIONID"])
        except (KeyError, TypeError, ValueError):
            continue
        if geometry.get("type") != "Point":
            continue
        found[location] = (
            location,
            (_text(row.get("ASSET_NAME")) or "").title(),
            _text(row.get("TYPE")),
            _text(row.get("ADDRESS")),
            _text(row.get("URL")),
            round(float(lon), 6),
            round(float(lat), 6),
        )
    return list(found.values())


def read_sessions(rows: list[dict]) -> list[tuple]:
    """(location ID, kind, title, day, start, end, minimum age, maximum age) for each
    skating or swimming drop-in session."""
    sessions = []
    for row in rows:
        kind = SECTIONS.get(_text(row.get("Section")) or "")
        if not kind:
            continue
        try:
            day = date.fromisoformat(str(row["First Date"])[:10])
            start = time(int(row["Start Hour"]), int(row["Start Minute"]))
            end = time(int(row["End Hour"]), int(row["End Min"]))
            location = int(row["Location ID"])
        except (KeyError, TypeError, ValueError):
            continue
        ages = []
        for field in ("Age Min", "Age Max"):
            try:
                ages.append(int(row[field]))
            except (KeyError, TypeError, ValueError):
                ages.append(None)
        sessions.append(
            (location, kind, _text(row["Course Title"]) or kind, day, start, end, *ages)
        )
    return sessions


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed"}
    with connect(database_url) as conn:
        try:
            facilities = read_facilities(_dump(client, FACILITIES_PACKAGE))
            sessions = read_sessions(_dump(client, PROGRAMS_PACKAGE, "Drop-in"))
            if len(facilities) < MIN_FACILITIES or len(sessions) < MIN_SESSIONS:
                raise FetchError(
                    f"Only {len(facilities)} facilities and {len(sessions)} sessions; not loaded"
                )
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM normalized.rec_sessions")
                cur.execute("DELETE FROM normalized.rec_facilities")
                with cur.copy(
                    """COPY normalized.rec_facilities (location_id, name, facility_type,
                           address, url, lon, lat) FROM STDIN"""
                ) as copy:
                    for row in facilities:
                        copy.write_row(row)
                with cur.copy(
                    """COPY normalized.rec_sessions (location_id, kind, title, day, starts,
                           ends, age_min, age_max) FROM STDIN"""
                ) as copy:
                    for row in sessions:
                        copy.write_row(row)
            result.update(status="loaded", facilities=len(facilities), sessions=len(sessions))
        except (FetchError, KeyError, ValueError, TypeError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.rec_refreshes (started_at, finished_at, status, facilities,
                   sessions, error) VALUES (%s, now(), %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result.get("facilities"),
                result.get("sessions"),
                result.get("error"),
            ),
        )
    return result


def day_sessions(conn, day: date) -> list[dict]:
    """Every location with skating or swimming on `day`, with its sessions in order."""
    places = {}
    for r in conn.execute(
        """SELECT f.location_id, f.name, f.address, f.url, f.lon, f.lat, s.kind, s.title,
                  s.starts, s.ends, s.age_min, s.age_max
           FROM normalized.rec_sessions s
           JOIN normalized.rec_facilities f USING (location_id)
           WHERE s.day = %s
           ORDER BY f.location_id, s.starts, s.title""",
        (day,),
    ):
        place = places.setdefault(
            r["location_id"],
            {
                "id": r["location_id"],
                "name": r["name"],
                "address": r["address"],
                "url": r["url"],
                "lon": r["lon"],
                "lat": r["lat"],
                "sessions": [],
            },
        )
        place["sessions"].append(
            {
                "kind": r["kind"],
                "title": r["title"],
                "starts": r["starts"].strftime("%H:%M"),
                "ends": r["ends"].strftime("%H:%M"),
                "ages": [r["age_min"], r["age_max"]],
            }
        )
    return list(places.values())
