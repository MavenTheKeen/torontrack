"""City of Toronto curbside collection: which day an address's garbage, Green Bin, recycling
and yard waste go out, for the map's address cards.

Two city datasets are combined. The daytime curbside collection areas map every address to
a calendar (Tuesday 1, Friday 2 and so on; published as a shapefile in latitude and
longitude, last redrawn in 2012 and still matching the calendar names). The pickup schedule
lists, for each calendar, every collection date of the year and what is collected; dates
are already moved for holidays. Each load replaces both.

Contains information licensed under the Open Government Licence – Toronto.
"""

import csv
import io
import json
import re
import struct
import zipfile
from datetime import UTC, date, datetime

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

SCHEDULE_PACKAGE = "solid-waste-pickup-schedule"
AREAS_PACKAGE = "solid-waste-daytime-curbside-collection-areas"
MAX_BYTES = 20 << 20
ITEMS = {
    "Garbage": "Garbage",
    "GreenBin": "Green Bin",
    "Recycling": "Recycling",
    "YardWaste": "Yard waste",
    "ChristmasTree": "Christmas trees",
}
NO_COLLECTION = "No Collection"
UPCOMING = 4
MIN_AREAS = 20


def _dbf_records(data: bytes) -> list[dict]:
    count, header, size = struct.unpack("<IHH", data[4:12])
    fields = []
    for offset in range(32, header - 1, 32):
        name = data[offset : offset + 11].split(b"\0")[0].decode("ascii")
        fields.append((name, data[offset + 16]))
    records = []
    for i in range(count):
        start = header + i * size + 1  # The first byte is the deletion flag.
        record, at = {}, start
        for name, length in fields:
            record[name] = data[at : at + length].decode("latin-1").strip()
            at += length
        records.append(record)
    return records


def _polygons(data: bytes) -> list[list[list[tuple[float, float]]]]:
    """The rings of every polygon record in a .shp file, as (lon, lat) points."""
    shapes, at = [], 100
    while at < len(data):
        _, words = struct.unpack(">ii", data[at : at + 8])
        body = data[at + 8 : at + 8 + 2 * words]
        at += 8 + 2 * words
        kind = struct.unpack("<i", body[:4])[0]
        if kind == 0:
            shapes.append([])
            continue
        if kind not in (5, 15, 25):
            raise ValueError(f"Shape type {kind} is not a polygon")
        parts, points = struct.unpack("<ii", body[36:44])
        starts = list(struct.unpack(f"<{parts}i", body[44 : 44 + 4 * parts])) + [points]
        xy = struct.unpack(f"<{2 * points}d", body[44 + 4 * parts : 44 + 4 * parts + 16 * points])
        coords = list(zip(xy[0::2], xy[1::2], strict=True))
        shapes.append([coords[a:b] for a, b in zip(starts, starts[1:], strict=False)])
    return shapes


def read_areas(archive: bytes) -> list[tuple]:
    """(area ID, calendar, district, bounding box, rings) from the collection areas zip.
    Calendars are spelled as in the schedule: 'Tuesday 1' -> 'Tuesday1'."""
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        names = {n.rsplit(".", 1)[-1].lower(): n for n in z.namelist()}
        records = _dbf_records(z.read(names["dbf"]))
        shapes = _polygons(z.read(names["shp"]))
    if len(records) != len(shapes):
        raise ValueError("Collection areas: shapes and records differ in number")
    areas = []
    for record, rings in zip(records, shapes, strict=True):
        if not rings:
            continue
        name = record["AREA_NAME"]
        calendar = name if name == NO_COLLECTION else name.replace(" ", "")
        points = [p for ring in rings for p in ring]
        lons, lats = [p[0] for p in points], [p[1] for p in points]
        box = (min(lons), min(lats), max(lons), max(lats))
        rounded = [[[round(x, 6), round(y, 6)] for x, y in ring] for ring in rings]
        areas.append((record["AREA_ID"], calendar, record.get("Area") or None, *box, rounded))
    return areas


def read_schedule(feed) -> list[tuple]:
    """(calendar, collection date, items) for each row of a pickup schedule CSV. Each row is
    one collection, on WeekStarting, already moved for holidays; an item is collected when
    its column holds a day letter rather than 0."""
    rows = []
    for row in csv.DictReader(feed):
        day = datetime.strptime(row["WeekStarting"].strip(), "%m/%d/%y").date()
        items = [label for column, label in ITEMS.items() if (row.get(column) or "0") != "0"]
        if items:
            rows.append((row["Calendar"].strip(), day, items))
    return rows


def _meta(client: FeedClient, package: str) -> dict:
    return json.loads(
        client.fetch_bytes(
            f"https://{STATIC_HOST}/api/3/action/package_show?id={package}", host=STATIC_HOST
        )
    )


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed"}
    with connect(database_url) as conn:
        try:
            areas_meta = _meta(client, AREAS_PACKAGE)
            [archive_url] = [
                r["url"]
                for r in areas_meta["result"]["resources"]
                if r.get("format", "").upper() == "SHP"
            ][:1]
            areas = read_areas(
                client.fetch_bytes(archive_url, host=STATIC_HOST, max_bytes=MAX_BYTES)
            )
            if len(areas) < MIN_AREAS:
                raise FetchError(f"Only {len(areas)} collection areas; not loaded")
            schedule = []
            for resource in _meta(client, SCHEDULE_PACKAGE)["result"]["resources"]:
                year = re.fullmatch(r"pickup-schedule-(\d{4})", resource.get("name", ""))
                if year and int(year[1]) >= started.year and resource.get("datastore_active"):
                    body = client.fetch_bytes(
                        f"https://{STATIC_HOST}/datastore/dump/{resource['id']}",
                        host=STATIC_HOST,
                        max_bytes=MAX_BYTES,
                    )
                    schedule += read_schedule(io.StringIO(body.decode("utf-8-sig")))
            if not schedule:
                raise FetchError(f"No pickup schedule for {started.year} or later")
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM normalized.waste_areas")
                cur.execute("DELETE FROM normalized.waste_collections")
                with cur.copy(
                    """COPY normalized.waste_areas (area_id, calendar, district, min_lon,
                           min_lat, max_lon, max_lat, rings) FROM STDIN"""
                ) as copy:
                    for *fields, rings in areas:
                        copy.write_row((*fields, json.dumps(rings)))
                with cur.copy(
                    "COPY normalized.waste_collections (calendar, collected_on, items) FROM STDIN"
                ) as copy:
                    for row in schedule:
                        copy.write_row(row)
            result.update(status="loaded", areas=len(areas), collections=len(schedule))
        except (FetchError, KeyError, ValueError, csv.Error, zipfile.BadZipFile) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.waste_refreshes (started_at, finished_at, status, areas,
                   collections, error) VALUES (%s, now(), %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result.get("areas"),
                result.get("collections"),
                result.get("error"),
            ),
        )
    return result


def _inside(lon: float, lat: float, rings: list) -> bool:
    """Even-odd rule over every ring, so holes are left out."""
    inside = False
    for ring in rings:
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1], strict=True):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


def collection(conn, lat: float, lon: float, today: date) -> dict | None:
    """The collection calendar for a point and its next UPCOMING collections from `today`,
    or None outside every collection area."""
    for area in conn.execute(
        """SELECT calendar, district, rings FROM normalized.waste_areas
           WHERE %s BETWEEN min_lon AND max_lon AND %s BETWEEN min_lat AND max_lat""",
        (lon, lat),
    ):
        if not _inside(lon, lat, area["rings"]):
            continue
        if area["calendar"] == NO_COLLECTION:
            return {"calendar": None, "district": area["district"], "upcoming": []}
        upcoming = conn.execute(
            """SELECT collected_on, items FROM normalized.waste_collections
               WHERE calendar = %s AND collected_on >= %s ORDER BY collected_on LIMIT %s""",
            (area["calendar"], today, UPCOMING),
        ).fetchall()
        return {"calendar": area["calendar"], "district": area["district"], "upcoming": upcoming}
    return None
