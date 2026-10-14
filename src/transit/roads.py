"""City of Toronto road restrictions: construction zones and road closures, with their
location, schedule and expected impact, from the city's open-data feed.

The feed is one JSON document (~4 MB, ~2,800 restrictions) that the city regenerates
continuously. Two quirks shape the parser: the body is not always valid JSON (a stray
backslash in a description, e.g. "Water \\ Sewer"), and several fields hold truncated
fragments of an internal JSON table, so those fields are ignored.

Contains information licensed under the Open Government Licence – Toronto.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from transit import road_routes
from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc import store

HOST = "secure.toronto.ca"
URL = f"https://{HOST}/opendata/cart/road_restrictions/v3?format=json"
RAW_KEEP = timedelta(days=7)
# A response listing fewer than this share of the restrictions currently in effect is
# treated as a partial feed: nothing is marked removed.
DROPOUT_SHARE = 0.5
DROPOUT_MIN = 200
# Generous bounds around the city; restrictions outside are skipped.
BOUNDS = (43.5, 43.9, -79.7, -79.05)
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
KINDS = {"CONSTRUCTION": "construction", "ROAD_CLOSED": "closure"}
# Every backslash escape, matched left to right so an escaped backslash is consumed whole.
ESCAPE = re.compile(r"\\(.)", re.DOTALL)
VALID_ESCAPES = set('"\\/bfnrtu')


def _repair(text: str) -> str:
    """Double any backslash that does not start a valid JSON escape."""
    return ESCAPE.sub(lambda m: m[0] if m[1] in VALID_ESCAPES else "\\\\" + m[1], text)


def decode(body: bytes) -> dict:
    text = body.decode("utf-8")
    try:
        payload = json.loads(text)
    except ValueError:
        try:
            payload = json.loads(_repair(text))
        except ValueError as exc:
            raise FetchError("Road restrictions response is not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("Closure"), list):
        raise FetchError("Road restrictions response has no Closure list")
    return payload


def _time(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _path(item: dict) -> list[list[float]] | None:
    """'[-79.36,43.66],[-79.35,43.66]' -> [[-79.36, 43.66], [-79.35, 43.66]]; falls back to
    the single point."""
    south, north, west, east = BOUNDS
    try:
        points = json.loads(f"[{item.get('geoPolyline') or ''}]")
        path = [[round(float(lon), 6), round(float(lat), 6)] for lon, lat in points]
    except (ValueError, TypeError):
        path = []
    if not path:
        try:
            path = [[float(item["longitude"]), float(item["latitude"])]]
        except (KeyError, TypeError, ValueError):
            return None
    if not all(west <= lon <= east and south <= lat <= north for lon, lat in path):
        return None
    return path


def _text(value) -> str | None:
    value = (value or "").strip() if isinstance(value, str) else None
    return value or None


def parse(payload: dict) -> tuple[list[dict], int]:
    """(restrictions, skipped). A restriction without an ID, kind, valid times or a
    location inside the city is skipped; duplicate IDs keep the most recently updated."""
    rows: dict[str, dict] = {}
    skipped = 0
    for item in payload["Closure"]:
        if not isinstance(item, dict):
            skipped += 1
            continue
        rid, kind = _text(item.get("id")), KINDS.get(item.get("type"))
        starts, ends, path = _time(item.get("startTime")), _time(item.get("endTime")), _path(item)
        if not (rid and kind and starts and ends and path) or ends < starts:
            skipped += 1
            continue
        row = {
            "restriction_id": rid,
            "kind": kind,
            "road": _text(item.get("road")),
            "name": _text(item.get("name")) or _text(item.get("road")) or rid,
            "district": _text(item.get("district")),
            "road_class": _text(item.get("roadClass")),
            "description": _text(item.get("description")),
            "directions": _text(item.get("directionsAffected")),
            "work_period": _text(item.get("workPeriod")),
            "max_impact": _text(item.get("maxImpact")),
            "current_impact": _text(item.get("currImpact")),
            "schedule": {
                day: _text(item.get(f"schedule{day.title()}")) for day in (*DAYS, "everyday")
            },
            "starts_at": starts,
            "ends_at": ends,
            "path": path,
            "source_updated_at": _time(item.get("lastUpdated")),
        }
        previous = rows.get(rid)
        if previous:
            skipped += 1
            newer = (row["source_updated_at"] or starts) >= (
                previous["source_updated_at"] or previous["starts_at"]
            )
            if not newer:
                continue
        rows[rid] = row
    return list(rows.values()), skipped


COLUMNS = (
    "kind",
    "road",
    "name",
    "district",
    "road_class",
    "description",
    "directions",
    "work_period",
    "max_impact",
    "current_impact",
    "schedule",
    "starts_at",
    "ends_at",
    "path",
    "source_updated_at",
)


def load(conn, rows: list[dict], response_id: int, now: datetime) -> str:
    """Upsert every restriction; mark those no longer listed as removed unless the
    response looks partial. Returns 'loaded' or 'dropout'."""
    in_effect = conn.execute(
        """SELECT count(*) AS n FROM normalized.road_restrictions
           WHERE removed_at IS NULL AND ends_at > %s""",
        (now,),
    ).fetchone()["n"]
    dropout = in_effect >= DROPOUT_MIN and len(rows) < in_effect * DROPOUT_SHARE
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO normalized.road_restrictions
                (restriction_id, {", ".join(COLUMNS)}, first_seen_at, last_seen_at, response_id)
                VALUES (%(restriction_id)s, {", ".join(f"%({c})s" for c in COLUMNS)},
                        %(now)s, %(now)s, %(response_id)s)
                ON CONFLICT (restriction_id) DO UPDATE SET
                {", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS)},
                last_seen_at = EXCLUDED.last_seen_at, removed_at = NULL,
                response_id = EXCLUDED.response_id""",
            [
                {
                    **r,
                    "schedule": json.dumps(r["schedule"]),
                    "path": json.dumps(r["path"]),
                    "now": now,
                    "response_id": response_id,
                }
                for r in rows
            ],
        )
        if not dropout:
            cur.execute(
                """UPDATE normalized.road_restrictions SET removed_at = %s
                   WHERE removed_at IS NULL AND NOT (restriction_id = ANY(%s))""",
                (now, [r["restriction_id"] for r in rows]),
            )
    return "dropout" if dropout else "loaded"


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed", "restrictions": None, "skipped": None}
    with connect(database_url) as conn:
        try:
            body = client.fetch_bytes(URL, host=HOST, max_bytes=40 * 1024 * 1024)
            digest = hashlib.sha256(body).hexdigest()
            latest = conn.execute(
                """SELECT sha256 FROM raw.road_restriction_responses
                   ORDER BY fetched_at DESC LIMIT 1"""
            ).fetchone()
            if latest and latest["sha256"] == digest:
                conn.execute(
                    """UPDATE normalized.road_restrictions SET last_seen_at = %s
                       WHERE removed_at IS NULL""",
                    (started,),
                )
                result.update(status="unchanged")
            else:
                rows, skipped = parse(decode(body))
                # A body seen before (the feed went back to an earlier state) is reused,
                # not stored twice, but still normalized: it differs from the latest.
                response_id = conn.execute(
                    """INSERT INTO raw.road_restriction_responses (fetched_at, sha256, body)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (sha256) DO UPDATE SET fetched_at = EXCLUDED.fetched_at
                       RETURNING response_id""",
                    (started, digest, body.decode("utf-8")),
                ).fetchone()["response_id"]
                status = load(conn, rows, response_id, started)
                result.update(status=status, restrictions=len(rows), skipped=skipped)
            # Which routes each new or changed restriction lies along. Derived data: a
            # failure is reported but does not fail the load; the next refresh retries.
            try:
                version = store.active_feed_version(conn)
                if version:
                    result["route_matches"] = road_routes.match(conn, version, started)
            except Exception as exc:
                result["route_matches"] = {"error": type(exc).__name__}
            conn.execute(
                """DELETE FROM raw.road_restriction_responses r WHERE fetched_at < %s
                   AND NOT EXISTS (SELECT 1 FROM normalized.road_restrictions n
                                   WHERE n.response_id = r.response_id)""",
                (started - RAW_KEEP,),
            )
        except (FetchError, UnicodeDecodeError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.road_restriction_refreshes
               (started_at, finished_at, status, restrictions, skipped, error)
               VALUES (%s, now(), %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["restrictions"],
                result["skipped"],
                result.get("error"),
            ),
        )
    return result


def active(conn, now: datetime) -> list[dict]:
    """Restrictions listed in the feed whose dates cover now, for the map, with the bus and
    streetcar routes each lies along under the current schedule."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT r.restriction_id, r.kind, r.road, r.name, r.description, r.directions,
                      r.work_period, r.max_impact, r.current_impact, r.schedule, r.starts_at,
                      r.ends_at, r.path,
                      coalesce(array_agg(DISTINCT m.route_id) FILTER
                               (WHERE m.route_id IS NOT NULL), '{}') AS routes
               FROM normalized.road_restrictions r
               LEFT JOIN normalized.road_restriction_routes m
                 ON m.restriction_id = r.restriction_id AND m.feed_version = (
                     SELECT feed_version FROM normalized.ttc_gtfs_versions
                     ORDER BY loaded_at DESC LIMIT 1)
               WHERE r.removed_at IS NULL AND r.starts_at <= %s AND r.ends_at > %s
               GROUP BY r.restriction_id
               ORDER BY r.restriction_id""",
            (now, now),
        )
    ]
