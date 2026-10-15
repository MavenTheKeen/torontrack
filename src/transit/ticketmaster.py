"""Concerts, games and shows in and around Toronto from Ticketmaster's Discovery API.

The city's events calendar (transit.events) lists festivals and community events but not
professional sports and few arena concerts. Ticketmaster sells tickets for nearly every
arena, stadium and concert hall in the city, so its Discovery API fills that gap.

- A free API key is needed (developer.ticketmaster.com), read from TICKETMASTER_API_KEY.
  Without one the refresh is recorded as skipped. The key travels only in the request URL;
  errors never include the URL, so it is not stored or logged.
- Events are requested one day at a time for DAYS days within RADIUS_KM of downtown. The
  API serves at most 1,000 results per query (size x page), and one day stays well under.
- Ticketmaster lists parking passes, suites and add-ons as separate events, and often lists
  one game or show several times (packages, presales). Add-ons are dropped by name and
  listings of one venue at one start time are merged, keeping the shortest name.
- The terms allow caching event content only for reasonable periods to provide the
  service: each load replaces every upcoming event, and past events are deleted after a day.
- Tickets are bought on Ticketmaster; the site links to each event's page.
"""

import json
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from transit.db import connect
from transit.http import FeedClient, FetchError

TORONTO = ZoneInfo("America/Toronto")
HOST = "app.ticketmaster.com"
URL = f"https://{HOST}/discovery/v2/events.json"
CENTRE = (43.6532, -79.3832)  # Toronto City Hall.
RADIUS_KM = 30
DAYS = 14
PAGE_SIZE = 200
MAX_PAGES = 5  # 1,000 results: the API's limit per query.
REQUEST_GAP_S = 0.25  # The API allows 5 requests a second.
BOUNDS = (43.4, 44.0, -79.9, -78.9)
ADD_ON = re.compile(
    r"\b(parking|suite|suites|upsell|add[- ]?on|vip package|hospitality package|"
    r"gift card|voucher|fast lane|early entry|tailgate)\b",
    re.IGNORECASE,
)


def api_key() -> str | None:
    return os.environ.get("TICKETMASTER_API_KEY", "").strip() or None


def request_url(key: str, day: date, page: int) -> str:
    """Events starting on `day`, from Toronto's midnight to the next."""
    start = datetime(day.year, day.month, day.day, tzinfo=TORONTO).astimezone(UTC)
    end = (datetime(day.year, day.month, day.day, tzinfo=TORONTO) + timedelta(days=1)).astimezone(
        UTC
    )
    params = {
        "apikey": key,
        "latlong": f"{CENTRE[0]},{CENTRE[1]}",
        "radius": RADIUS_KM,
        "unit": "km",
        "startDateTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "size": PAGE_SIZE,
        "page": page,
        "sort": "date,asc",
        "locale": "*",
    }
    return f"{URL}?{urlencode(params)}"


def parse_event(event: dict) -> tuple[dict | None, str | None]:
    """(event row, or None and why it was skipped)."""
    name = " ".join((event.get("name") or "").split())
    if not event.get("id") or not name:
        return None, "no_id_or_name"
    if ADD_ON.search(name):
        return None, "add_on"
    dates = event.get("dates") or {}
    status = ((dates.get("status") or {}).get("code") or "onsale").lower()
    if status == "cancelled":
        return None, "cancelled"
    start = dates.get("start") or {}
    try:
        local_date = date.fromisoformat(start["localDate"])
    except (KeyError, TypeError, ValueError):
        return None, "no_date"
    time_tbd = bool(
        start.get("timeTBA") or start.get("noSpecificTime") or start.get("dateTBD")
        or not start.get("dateTime")
    )  # fmt: skip
    starts_at = None
    if not time_tbd:
        try:
            starts_at = datetime.fromisoformat(start["dateTime"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            time_tbd = True
    venues = (event.get("_embedded") or {}).get("venues") or []
    venue = venues[0] if venues else {}
    try:
        lat = float(venue["location"]["latitude"])
        lon = float(venue["location"]["longitude"])
    except (KeyError, TypeError, ValueError):
        return None, "no_location"
    south, north, west, east = BOUNDS
    if not (south <= lat <= north and west <= lon <= east):
        return None, "outside_area"
    classification = next(
        (c for c in event.get("classifications") or [] if c.get("primary")),
        (event.get("classifications") or [{}])[0],
    )
    url = event.get("url")
    return {
        "event_id": event["id"],
        "name": name,
        "segment": (classification.get("segment") or {}).get("name"),
        "genre": (classification.get("genre") or {}).get("name"),
        "starts_at": starts_at,
        "local_date": local_date,
        "time_tbd": time_tbd,
        "status": status,
        "venue_id": venue.get("id"),
        "venue": " ".join((venue.get("name") or "").split()) or None,
        "address": ((venue.get("address") or {}).get("line1") or "").strip() or None,
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "url": url if isinstance(url, str) and url.startswith("https://") else None,
    }, None


def merge(rows: list[dict], skipped: dict) -> list[dict]:
    """One row per venue and start: the same game or show listed several times (packages,
    presales) keeps the listing with the shortest name."""
    best: dict[tuple, dict] = {}
    for row in rows:
        when = row["starts_at"] or row["local_date"]
        key = (row["venue_id"] or row["venue"], when) if not row["time_tbd"] else (
            row["venue_id"] or row["venue"], when, row["name"].casefold()
        )  # fmt: skip
        if key in best:
            skipped["duplicate_listing"] = skipped.get("duplicate_listing", 0) + 1
            if len(row["name"]) < len(best[key]["name"]):
                best[key] = row
        else:
            best[key] = row
    return sorted(
        best.values(),
        key=lambda r: (
            r["local_date"],
            r["starts_at"] or datetime.max.replace(tzinfo=UTC),
            r["name"],
        ),
    )


def read(
    client: FeedClient, key: str, today: date, sleep=time.sleep
) -> tuple[list[dict], dict, int]:
    """(merged events for DAYS days from `today`, skipped counts, requests made)."""
    rows, skipped, requests = [], {}, 0
    for offset in range(DAYS):
        day = today + timedelta(days=offset)
        for page in range(MAX_PAGES):
            if requests:
                sleep(REQUEST_GAP_S)
            body = client.fetch_bytes(request_url(key, day, page), host=HOST)
            requests += 1
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise FetchError("Ticketmaster returned invalid JSON") from exc
            for event in (payload.get("_embedded") or {}).get("events") or []:
                row, why = parse_event(event)
                if row is None:
                    skipped[why] = skipped.get(why, 0) + 1
                else:
                    rows.append(row)
            info = payload.get("page") or {}
            if info.get("number", page) + 1 >= info.get("totalPages", 0):
                break
    return merge(rows, skipped), skipped, requests


COLUMNS = (
    "event_id", "name", "segment", "genre", "starts_at", "local_date", "time_tbd", "status",
    "venue_id", "venue", "address", "lat", "lon", "url",
)  # fmt: skip


def load(conn, rows: list[dict], today: date, now: datetime) -> None:
    """Replace upcoming events; drop those that ended over a day ago."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM normalized.ticketed_events WHERE local_date >= %s OR local_date < %s",
            (today, today - timedelta(days=1)),
        )
        cur.executemany(
            f"""INSERT INTO normalized.ticketed_events ({", ".join(COLUMNS)}, loaded_at)
                VALUES ({", ".join(f"%({c})s" for c in COLUMNS)}, %(now)s)
                ON CONFLICT (event_id) DO NOTHING""",
            [{**r, "now": now} for r in rows],
        )


def refresh(
    database_url: str, client: FeedClient, now: datetime | None = None, sleep=time.sleep
) -> dict:
    started = now or datetime.now(UTC)
    today = started.astimezone(TORONTO).date()
    key = api_key()
    result = {"status": "failed", "requests": 0, "events": None}
    with connect(database_url) as conn:
        if key is None:
            result.update(status="skipped", error="TICKETMASTER_API_KEY is not set")
        else:
            try:
                rows, skipped, requests = read(client, key, today, sleep)
                load(conn, rows, today, started)
                result.update(status="loaded", requests=requests, events=len(rows), skipped=skipped)
            except (FetchError, KeyError, ValueError, TypeError) as exc:
                result["error"] = str(exc)[:1000].replace(key, "<key>")
        conn.execute(
            """INSERT INTO ops.ticketed_event_refreshes (started_at, finished_at, status,
                   requests, events, skipped, error)
               VALUES (%s, now(), %s, %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["requests"],
                result["events"],
                json.dumps(result["skipped"]) if result.get("skipped") else None,
                result.get("error"),
            ),
        )
    return result
