"""City wards and 311 service requests, for "My street" and the findings.

The city publishes every 311 service request since 2010, one file per year, refreshed about
monthly. A request carries no address: only its ward, the first three characters of the
postal code and sometimes an intersection. They are kept as daily counts per ward and
request type, which is all the site shows; no row identifies a caller or a household.
Years from FIRST_YEAR on use the current 25 wards and are loaded; the current year is
fetched on every run and skipped when unchanged, earlier years once.

Contains information licensed under the Open Government Licence – Toronto.
"""

import csv
import hashlib
import json
import math
import re
import tempfile
import zipfile
from collections import Counter
from datetime import UTC, date, datetime, timedelta

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST
from transit.waste import _inside

REQUESTS_PACKAGE = "311-service-requests-customer-initiated"
WARDS_PACKAGE = "city-wards"
FIRST_YEAR = 2021
MAX_BYTES = 100 << 20
YEAR_NAME = re.compile(r"311 service requests (\d{4})", re.IGNORECASE)
WARD = re.compile(r"\((\d{1,2})\)\s*$")
UNKNOWN_WARD = 0
RECENT_DAYS = 30
TOP_TYPES = 5
NEARBY_M = 500
WARD_COUNT = 25


def read_wards(payload: list[dict]) -> list[tuple]:
    """(ward number, name, bounding box, rings) from the city wards datastore rows."""
    wards = []
    for row in payload:
        geometry = json.loads(row["geometry"])
        polygons = (
            [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        )
        rings = [[[round(x, 6), round(y, 6)] for x, y, *_ in ring] for p in polygons for ring in p]
        points = [p for ring in rings for p in ring]
        lons, lats = [p[0] for p in points], [p[1] for p in points]
        box = (min(lons), min(lats), max(lons), max(lats))
        wards.append((int(row["AREA_SHORT_CODE"]), row["AREA_NAME"], *box, rings))
    return wards


def decoded_lines(raw):
    """Text lines from a binary file whose years differ in encoding: UTF-8, or Windows-1252
    for a line that is not valid UTF-8 (the 2026 file spells CaféTO in Windows-1252)."""
    for line in raw:
        try:
            yield line.decode("utf-8-sig")
        except UnicodeDecodeError:
            yield line.decode("cp1252", errors="replace")


def read_requests(feed) -> tuple[Counter, int]:
    """Requests counted by (day, ward number, request type, division) from one year's CSV.
    Rows without a date or type are skipped and counted."""
    counts, skipped = Counter(), 0
    for row in csv.DictReader(feed):
        try:
            day = date.fromisoformat(row["Creation Date"][:10])
        except (KeyError, ValueError):
            skipped += 1
            continue
        kind = (row.get("Service Request Type") or "").strip()
        if not kind:
            skipped += 1
            continue
        ward = WARD.search(row.get("Ward") or "")
        counts[
            (day, int(ward[1]) if ward else UNKNOWN_WARD, kind, (row.get("Division") or "").strip())
        ] += 1
    return counts, skipped


def _meta(client: FeedClient, package: str) -> dict:
    return json.loads(
        client.fetch_bytes(
            f"https://{STATIC_HOST}/api/3/action/package_show?id={package}", host=STATIC_HOST
        )
    )


def _load_wards(conn, client: FeedClient) -> int:
    meta = _meta(client, WARDS_PACKAGE)
    [resource] = [r for r in meta["result"]["resources"] if r.get("datastore_active")][:1]
    body = client.fetch_bytes(
        f"https://{STATIC_HOST}/datastore/dump/{resource['id']}?format=json",
        host=STATIC_HOST,
        max_bytes=MAX_BYTES,
    )
    payload = json.loads(body)
    fields = [f["id"] for f in payload["fields"]]
    wards = read_wards([dict(zip(fields, r, strict=True)) for r in payload["records"]])
    if len(wards) != WARD_COUNT:
        raise FetchError(f"City wards lists {len(wards)} wards, not {WARD_COUNT}; not loaded")
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM normalized.city_wards")
        with cur.copy(
            """COPY normalized.city_wards (ward, name, min_lon, min_lat, max_lon, max_lat,
                   rings) FROM STDIN"""
        ) as copy:
            for *fields_, rings in wards:
                copy.write_row((*fields_, json.dumps(rings)))
    return len(wards)


def _load_year(conn, client: FeedClient, year: int, url: str, now: datetime) -> dict | None:
    """Load one year's file unless its hash matches the last load; None when unchanged."""
    with tempfile.TemporaryFile() as archive:
        client.fetch_bytes(url, host=STATIC_HOST, max_bytes=MAX_BYTES, sink=archive)
        archive.seek(0)
        digest = hashlib.file_digest(archive, "sha256").hexdigest()
        last = conn.execute(
            "SELECT sha256 FROM ops.service_request_files WHERE year = %s", (year,)
        ).fetchone()
        if last and last["sha256"] == digest:
            return None
        archive.seek(0)
        with zipfile.ZipFile(archive) as z:
            [name] = [n for n in z.namelist() if n.lower().endswith(".csv")][:1]
            with z.open(name) as raw:
                counts, skipped = read_requests(decoded_lines(raw))
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """DELETE FROM normalized.service_requests_daily
               WHERE day >= %s AND day < %s""",
            (date(year, 1, 1), date(year + 1, 1, 1)),
        )
        with cur.copy(
            """COPY normalized.service_requests_daily (day, ward, request_type, division,
                   requests) FROM STDIN"""
        ) as copy:
            for (day, ward, kind, division), n in counts.items():
                if day.year == year:
                    copy.write_row((day, ward, kind, division, n))
        cur.execute(
            """INSERT INTO ops.service_request_files (year, sha256, loaded_at, requests, skipped)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (year) DO UPDATE SET sha256 = EXCLUDED.sha256,
                 loaded_at = EXCLUDED.loaded_at, requests = EXCLUDED.requests,
                 skipped = EXCLUDED.skipped""",
            (year, digest, now, sum(counts.values()), skipped),
        )
    return {"year": year, "requests": sum(counts.values()), "skipped": skipped}


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed", "years": []}
    with connect(database_url) as conn:
        try:
            result["wards"] = _load_wards(conn, client)
            loaded = {r["year"] for r in conn.execute("SELECT year FROM ops.service_request_files")}
            for resource in _meta(client, REQUESTS_PACKAGE)["result"]["resources"]:
                match = YEAR_NAME.fullmatch(resource.get("name", "").strip())
                if not match:
                    continue
                year = int(match[1])
                if year < FIRST_YEAR or (year in loaded and year < started.year):
                    continue
                done = _load_year(conn, client, year, resource["url"], started)
                if done:
                    result["years"].append(done)
            result["status"] = "loaded" if result["years"] else "unchanged"
        except (FetchError, KeyError, ValueError, csv.Error, zipfile.BadZipFile) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.service_request_refreshes (started_at, finished_at, status,
                   years, error) VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], json.dumps(result["years"]), result.get("error")),
        )
    return result


def ward_at(conn, lat: float, lon: float) -> dict | None:
    for ward in conn.execute(
        """SELECT ward, name, rings FROM normalized.city_wards
           WHERE %s BETWEEN min_lon AND max_lon AND %s BETWEEN min_lat AND max_lat""",
        (lon, lat),
    ):
        if _inside(lon, lat, ward["rings"]):
            return {"number": ward["ward"], "name": ward["name"]}
    return None


def ward_requests(conn, ward: int) -> dict:
    """The ward's most common request types in the RECENT_DAYS up to the newest day loaded,
    each with the share of the city's requests of that type that came from the ward."""
    latest = conn.execute("SELECT max(day) AS day FROM normalized.service_requests_daily")
    latest = latest.fetchone()["day"]
    if latest is None:
        return {"through": None, "days": RECENT_DAYS, "total": 0, "top": []}
    since = latest - timedelta(days=RECENT_DAYS - 1)
    rows = conn.execute(
        """SELECT request_type,
                  sum(requests) FILTER (WHERE ward = %s) AS here,
                  sum(requests) AS city
           FROM normalized.service_requests_daily
           WHERE day BETWEEN %s AND %s
           GROUP BY request_type
           HAVING sum(requests) FILTER (WHERE ward = %s) > 0
           ORDER BY here DESC, request_type LIMIT %s""",
        (ward, since, latest, ward, TOP_TYPES),
    ).fetchall()
    total = conn.execute(
        """SELECT coalesce(sum(requests), 0) AS n FROM normalized.service_requests_daily
           WHERE ward = %s AND day BETWEEN %s AND %s""",
        (ward, since, latest),
    ).fetchone()["n"]
    return {
        "through": latest,
        "days": RECENT_DAYS,
        "total": total,
        "top": [
            {"type": r["request_type"], "requests": r["here"], "city": r["city"]} for r in rows
        ],
    }


def _metres_to_path(lat: float, lon: float, path: list) -> float:
    """Shortest distance from a point to a polyline of [lon, lat], on a local flat plane."""
    scale = math.cos(math.radians(lat))

    def xy(p):
        return ((p[0] - lon) * scale * 111_195, (p[1] - lat) * 111_195)

    points = [xy(p) for p in path]
    if len(points) == 1:
        return math.hypot(*points[0])
    best = math.inf
    for (x1, y1), (x2, y2) in zip(points, points[1:], strict=False):
        dx, dy = x2 - x1, y2 - y1
        length = dx * dx + dy * dy
        t = 0.0 if length == 0 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length))
        best = min(best, math.hypot(x1 + t * dx, y1 + t * dy))
    return best


def nearby_restrictions(restrictions: list[dict], lat: float, lon: float) -> list[dict]:
    """Road restrictions (transit.roads.active) within NEARBY_M of a point, nearest first."""
    found = []
    for r in restrictions:
        metres = _metres_to_path(lat, lon, r["path"])
        if metres <= NEARBY_M:
            found.append(
                {
                    "id": r["restriction_id"],
                    "kind": r["kind"],
                    "name": r["name"],
                    "ends_at": r["ends_at"],
                    "metres": round(metres / 10) * 10,
                }
            )
    return sorted(found, key=lambda r: r["metres"])
