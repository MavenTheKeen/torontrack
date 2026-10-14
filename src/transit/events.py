"""City of Toronto festivals and events, for the map and station panels.

The city's calendar feed ("Festivals & Events", real time) is one JSON document of about
225 MB: every event in its calendar with descriptions, image metadata and repeated date
lists. Loading it whole takes over a gigabyte of memory, so it is streamed to a temporary
file and read event by event (ijson). Only what the site shows is kept: name, categories,
venue and location, website, and each date in a window from yesterday to WINDOW_DAYS ahead.

The calendar is the city's own listing: festivals, markets, exhibits, parades, runs and
community events. It does not list professional sports and lists few arena concerts, so it
is context for riders, not a measure of crowds.

Contains information licensed under the Open Government Licence – Toronto.
"""

import hashlib
import json
import tempfile
from datetime import UTC, datetime, timedelta

import ijson

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "festivals-events"
RESOURCE = "Festivals and events json feed"
WINDOW_DAYS = 60
MAX_BYTES = 600 << 20
BOUNDS = (43.5, 43.9, -79.7, -79.05)


def _location(event: dict) -> dict | None:
    """The first listed location with coordinates inside the city."""
    south, north, west, east = BOUNDS
    for loc in event.get("event_locations") or []:
        try:
            gps = json.loads(loc.get("location_gps") or "[]")
            lat, lon = float(gps[0]["gps_lat"]), float(gps[0]["gps_lng"])
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if south <= lat <= north and west <= lon <= east:
            return {
                "venue": (loc.get("location_name") or "").strip() or None,
                "address": (loc.get("location_address") or "").strip() or None,
                "lat": round(lat, 6),
                "lon": round(lon, 6),
            }
    return None


def parse_event(event: dict, start: datetime, end: datetime) -> tuple[dict | None, str | None]:
    """(event row with its dates in the window, or None and why it was skipped)."""
    if event.get("event_status") != "Approved":
        return None, "not_approved"
    if not event.get("id") or not (event.get("event_name") or "").strip():
        return None, "no_id_or_name"
    dates = set()
    for d in event.get("event_dates") or []:
        try:
            at = datetime.fromtimestamp(int(d["date"]) / 1000, UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        if start <= at < end:
            dates.add(at)
    if not dates:
        return None, "no_dates_in_window"
    location = _location(event)
    if location is None:
        return None, "no_location"
    try:
        modified = datetime.fromisoformat(event["__ModifiedOn"])
    except (KeyError, TypeError, ValueError):
        modified = None
    website = (event.get("event_website") or "").strip() or None
    if website and not website.startswith(("http://", "https://")):
        website = f"https://{website}"
    return {
        "event_id": event["id"],
        "name": " ".join(event["event_name"].split()),
        "categories": [c for c in event.get("event_category") or [] if isinstance(c, str)],
        "featured": event.get("featured_event") == "Yes",
        "free": {"Yes": True, "No": False}.get(event.get("free_event")),
        "website": website,
        "source_modified_at": modified,
        "dates": sorted(dates),
        **location,
    }, None


def read(feed, now: datetime) -> tuple[list[dict], dict]:
    """Events with dates from yesterday to WINDOW_DAYS ahead, streamed from the file.

    The feed repeats each event once per date (up to 100 copies with different IDs and
    the same full date list: 3,703 records for 308 events in October 2026), so copies with
    the same name and location are merged under an ID derived from those, and their dates
    combined."""
    start, end = now - timedelta(days=1), now + timedelta(days=WINDOW_DAYS)
    merged: dict[str, dict] = {}
    skipped: dict[str, int] = {}
    try:
        for event in ijson.items(feed, "value.item"):
            row, why = parse_event(event, start, end)
            if row is None:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            key = f"{row['name'].casefold()}|{row['lat']:.5f}|{row['lon']:.5f}"
            event_id = hashlib.sha256(key.encode()).hexdigest()[:20]
            if event_id in merged:
                skipped["duplicate_copies"] = skipped.get("duplicate_copies", 0) + 1
                dates = set(merged[event_id]["dates"]) | set(row["dates"])
                merged[event_id]["dates"] = sorted(dates)
            else:
                merged[event_id] = {**row, "event_id": event_id}
    except ijson.JSONError as exc:
        raise FetchError("Events feed is not valid JSON") from exc
    if not merged:
        raise FetchError("Events feed has no events in the window")
    return list(merged.values()), skipped


COLUMNS = (
    "name",
    "categories",
    "featured",
    "free",
    "website",
    "venue",
    "address",
    "lat",
    "lon",
    "source_modified_at",
)


def load(conn, rows: list[dict], now: datetime) -> int:
    """Upsert events and replace every date from now on; past dates stay as history."""
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO normalized.city_events (event_id, {", ".join(COLUMNS)}, last_seen_at)
                VALUES (%(event_id)s, {", ".join(f"%({c})s" for c in COLUMNS)}, %(now)s)
                ON CONFLICT (event_id) DO UPDATE SET
                {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS)},
                last_seen_at = EXCLUDED.last_seen_at""",
            [{**r, "now": now} for r in rows],
        )
        cur.execute("DELETE FROM normalized.city_event_occurrences WHERE starts_at >= %s", (now,))
        with cur.copy(
            "COPY normalized.city_event_occurrences (event_id, starts_at) FROM STDIN"
        ) as copy:
            count = 0
            for r in rows:
                for at in r["dates"]:
                    if at >= now:
                        copy.write_row((r["event_id"], at))
                        count += 1
        # Yesterday's dates are inserted only if not already there.
        cur.executemany(
            """INSERT INTO normalized.city_event_occurrences (event_id, starts_at)
               VALUES (%s, %s) ON CONFLICT DO NOTHING""",
            [(r["event_id"], at) for r in rows for at in r["dates"] if at < now],
        )
    return count


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed", "feed_sha256": None, "events": None, "occurrences": None}
    with connect(database_url) as conn:
        try:
            meta = json.loads(
                client.fetch_bytes(
                    f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}",
                    host=STATIC_HOST,
                )
            )
            urls = {r["name"]: r["url"] for r in meta["result"]["resources"]}
            if RESOURCE not in urls:
                raise FetchError(f"Events resource {RESOURCE!r} not found")
            with tempfile.TemporaryFile() as feed:
                client.fetch_bytes(urls[RESOURCE], host=STATIC_HOST, max_bytes=MAX_BYTES, sink=feed)
                feed.seek(0)
                digest = hashlib.file_digest(feed, "sha256").hexdigest()
                result["feed_sha256"] = digest
                last = conn.execute(
                    """SELECT feed_sha256 FROM ops.city_event_refreshes
                       WHERE status = 'loaded' ORDER BY refresh_id DESC LIMIT 1"""
                ).fetchone()
                if last and last["feed_sha256"] == digest:
                    result["status"] = "unchanged"
                else:
                    feed.seek(0)
                    rows, skipped = read(feed, started)
                    occurrences = load(conn, rows, started)
                    result.update(
                        status="loaded", events=len(rows), occurrences=occurrences, skipped=skipped
                    )
        except (FetchError, KeyError, ValueError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.city_event_refreshes (started_at, finished_at, status,
                   feed_sha256, events, occurrences, skipped, error)
               VALUES (%s, now(), %s, %s, %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["feed_sha256"],
                result["events"],
                result["occurrences"],
                json.dumps(result["skipped"]) if result.get("skipped") else None,
                result.get("error"),
            ),
        )
    return result


def upcoming(conn, now: datetime, days: int = 7) -> list[dict]:
    """Events with a date from the start of today (Toronto) to `days` ahead, each with its
    dates in that range."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT e.event_id, e.name, e.categories, e.featured, e.free, e.website,
                      e.venue, e.address, e.lat, e.lon,
                      array_agg(o.starts_at ORDER BY o.starts_at) AS dates
               FROM normalized.city_events e
               JOIN normalized.city_event_occurrences o USING (event_id)
               WHERE o.starts_at >= date_trunc('day', %(now)s AT TIME ZONE 'America/Toronto')
                                    AT TIME ZONE 'America/Toronto'
                 AND o.starts_at < %(now)s + make_interval(days => %(days)s)
               GROUP BY e.event_id
               ORDER BY min(o.starts_at), e.name""",
            {"now": now, "days": days},
        )
    ]
