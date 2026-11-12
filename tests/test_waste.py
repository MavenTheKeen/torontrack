"""City of Toronto curbside collection: reading the areas and schedule, and lookups."""

import io
import json
import struct
import zipfile
from datetime import UTC, date, datetime

import pytest

from transit import waste
from transit.db import connect

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)


def shp(polygons) -> bytes:
    """A polygon .shp file: each polygon a list of rings of (lon, lat)."""
    records = b""
    for number, rings in enumerate(polygons, 1):
        points = [p for ring in rings for p in ring]
        starts, at = [], 0
        for ring in rings:
            starts.append(at)
            at += len(ring)
        body = struct.pack("<i4d", 5, 0, 0, 0, 0)
        body += struct.pack("<ii", len(rings), len(points))
        body += struct.pack(f"<{len(starts)}i", *starts)
        body += b"".join(struct.pack("<2d", *p) for p in points)
        records += struct.pack(">ii", number, len(body) // 2) + body
    header = struct.pack(">i20xi", 9994, (100 + len(records)) // 2)
    header += struct.pack("<ii4d32x", 1000, 5, 0, 0, 0, 0)
    return header + records


def dbf(rows) -> bytes:
    fields = [("AREA_ID", 12), ("AREA_LONG_", 20), ("AREA_NAME", 200), ("Area", 50)]
    size = 1 + sum(length for _, length in fields)
    header_size = 32 + 32 * len(fields) + 1
    out = struct.pack("<B3BIHH20x", 3, 26, 11, 12, len(rows), header_size, size)
    for name, length in fields:
        out += name.encode().ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([length]) + b"\0" * 15
    out += b"\r"
    for row in rows:
        out += b" " + b"".join(v.encode().ljust(n) for v, (_, n) in zip(row, fields, strict=True))
    return out + b"\x1a"


SQUARE = [(-79.41, 43.66), (-79.39, 43.66), (-79.39, 43.68), (-79.41, 43.68), (-79.41, 43.66)]
HOLE = [(-79.402, 43.668), (-79.398, 43.668), (-79.398, 43.672), (-79.402, 43.672)]
DOWNTOWN = [(-79.39, 43.64), (-79.37, 43.64), (-79.37, 43.66), (-79.39, 43.66)]


def archive() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("areas.shp", shp([[SQUARE, HOLE], [DOWNTOWN]]))
        z.writestr("areas.dbf", dbf([
            ("1", "D3TU2", "Tuesday 2", "District 3"),
            ("2", "D2NIT", "No Collection", "District 2"),
        ]))  # fmt: skip
    return out.getvalue()


SCHEDULE = """_id,Calendar,WeekStarting,GreenBin,Garbage,Recycling,YardWaste,ChristmasTree
1,Tuesday2,11/10/26,T,T,0,T,0
2,Tuesday2,11/17/26,T,0,T,0,0
3,Tuesday2,12/26/26,S,S,0,0,0
4,Tuesday2,12/29/26,0,0,0,0,0
"""


def test_areas_are_read_from_the_shapefile():
    areas = waste.read_areas(archive())
    assert [(a[0], a[1], a[2]) for a in areas] == [
        ("1", "Tuesday2", "District 3"),
        ("2", "No Collection", "District 2"),
    ]
    assert areas[0][3:7] == (-79.41, 43.66, -79.39, 43.68)
    rings = areas[0][7]
    assert len(rings) == 2 and rings[0][0] == [-79.41, 43.66]
    assert waste._inside(-79.405, 43.665, rings)
    assert not waste._inside(-79.4, 43.67, rings)  # In the hole.
    assert not waste._inside(-79.38, 43.665, rings)


def test_schedule_rows_are_collections_with_their_items():
    assert waste.read_schedule(io.StringIO(SCHEDULE)) == [
        ("Tuesday2", date(2026, 11, 10), ["Garbage", "Green Bin", "Yard waste"]),
        ("Tuesday2", date(2026, 11, 17), ["Green Bin", "Recycling"]),
        ("Tuesday2", date(2026, 12, 26), ["Garbage", "Green Bin"]),
    ]


class FakeClient:
    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None, headers=None):
        if url.endswith(f"id={waste.AREAS_PACKAGE}"):
            resources = [{"format": "SHP", "url": f"https://{host}/areas.zip"}]
        elif url.endswith(f"id={waste.SCHEDULE_PACKAGE}"):
            resources = [
                {"name": "pickup-schedule-2025", "id": "old", "datastore_active": True},
                {"name": "pickup-schedule-2026", "id": "now", "datastore_active": True},
            ]
        elif url.endswith("/areas.zip"):
            return archive()
        else:
            assert url.endswith("/datastore/dump/now")
            return SCHEDULE.encode()
        return json.dumps({"result": {"resources": resources}}).encode()


@pytest.mark.integration
def test_refresh_and_lookup(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    monkeypatch.setattr(waste, "MIN_AREAS", 2)
    url = settings.database_url
    assert waste.refresh(url, FakeClient(), NOW) == {
        "status": "loaded",
        "areas": 2,
        "collections": 3,
    }
    with connect(url) as conn:
        found = waste.collection(conn, 43.665, -79.405, date(2026, 11, 12))
    assert found["calendar"] == "Tuesday2"
    assert [u["collected_on"] for u in found["upcoming"]] == [
        date(2026, 11, 17),
        date(2026, 12, 26),
    ]
    client = TestClient(create_app(Settings(url), clock=lambda: NOW))
    night = client.get("/api/waste", params={"at": "43.65,-79.38"}).json()["collection"]
    assert night == {"calendar": None, "district": "District 2", "upcoming": []}
    assert client.get("/api/waste", params={"at": "43.70,-79.30"}).json()["collection"] is None
    assert client.get("/api/waste", params={"at": "nowhere"}).status_code == 422
