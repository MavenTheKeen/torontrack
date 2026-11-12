"""City wards and 311 service requests: reading, loading, and the My street summary."""

import io
import json
import zipfile
from datetime import UTC, date, datetime

import pytest

from transit import neighbourhood
from transit.db import connect

NOW = datetime(2026, 11, 12, 15, 0, tzinfo=UTC)
HEADER = (
    "Creation Date,Status,First 3 Chars of Postal Code,Intersection Street 1,"
    "Intersection Street 2,Ward,Service Request Type,Division,Section\n"
)


def requests_csv(rows) -> str:
    return HEADER + "".join(
        f"{day} 08:00:00.0000000,Completed,M6G,,,{ward},{kind},Transportation Services,X\n"
        for day, ward, kind in rows
    )


ROWS = [
    ("2026-10-30", "Davenport (09)", "Road - Pot hole"),
    ("2026-10-30", "Davenport (09)", "Road - Pot hole"),
    ("2026-10-31", "Davenport (09)", "Graffiti"),
    ("2026-10-31", "University-Rosedale (11)", "Road - Pot hole"),
    ("2026-10-31", "", "Graffiti"),
    ("2026-09-01", "Davenport (09)", "Graffiti"),
    ("not a date", "Davenport (09)", "Graffiti"),
]
WARD_ROWS = [
    {"AREA_SHORT_CODE": "09", "AREA_NAME": "Davenport", "geometry": json.dumps({
        "type": "Polygon",
        "coordinates": [[[-79.45, 43.66], [-79.42, 43.66], [-79.42, 43.68], [-79.45, 43.68],
                         [-79.45, 43.66]]]})},
    {"AREA_SHORT_CODE": "11", "AREA_NAME": "University-Rosedale", "geometry": json.dumps({
        "type": "MultiPolygon",
        "coordinates": [[[[-79.42, 43.66], [-79.38, 43.66], [-79.38, 43.68], [-79.42, 43.66]]]]})},
]  # fmt: skip


def test_requests_are_counted_by_day_ward_and_type():
    counts, skipped = neighbourhood.read_requests(io.StringIO(requests_csv(ROWS)))
    assert skipped == 1
    assert counts[(date(2026, 10, 30), 9, "Road - Pot hole", "Transportation Services")] == 2
    assert counts[(date(2026, 10, 31), 0, "Graffiti", "Transportation Services")] == 1
    assert sum(counts.values()) == 6


def test_lines_in_either_encoding_are_read():
    raw = io.BytesIO("a,CaféTO\n".encode() + "b,CaféTO\n".encode("cp1252"))
    assert list(neighbourhood.decoded_lines(raw)) == ["a,CaféTO\n", "b,CaféTO\n"]


def test_wards_are_read_from_polygons_and_multipolygons():
    wards = neighbourhood.read_wards(WARD_ROWS)
    assert [(w[0], w[1]) for w in wards] == [(9, "Davenport"), (11, "University-Rosedale")]
    assert wards[1][2:6] == (-79.42, 43.66, -79.38, 43.68)


def test_road_work_within_500_m_nearest_first():
    work = [
        {"restriction_id": "far", "kind": "closure", "name": "Far St", "ends_at": NOW,
         "path": [[-79.40, 43.70], [-79.39, 43.70]]},
        {"restriction_id": "near", "kind": "construction", "name": "Near St", "ends_at": NOW,
         "path": [[-79.4301, 43.6705], [-79.4301, 43.6695]]},  # passes about 8 m east
        {"restriction_id": "point", "kind": "closure", "name": "Corner", "ends_at": NOW,
         "path": [[-79.43, 43.673]]},  # about 330 m north
    ]  # fmt: skip
    found = neighbourhood.nearby_restrictions(work, 43.67, -79.4302)
    assert [(r["id"], r["metres"]) for r in found] == [("near", 10), ("point", 330)]


class FakeClient:
    def __init__(self, files):
        self.files = files

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None, headers=None):
        if url.endswith(f"id={neighbourhood.WARDS_PACKAGE}"):
            body = {"result": {"resources": [{"id": "wards", "datastore_active": True}]}}
        elif url.endswith(f"id={neighbourhood.REQUESTS_PACKAGE}"):
            body = {"result": {"resources": [
                {"name": f"311 Service Requests {year}", "url": f"https://{host}/sr{year}.zip"}
                for year in (2020, 2025, 2026)
            ]}}  # fmt: skip
        elif "/datastore/dump/wards" in url:
            fields = ["AREA_SHORT_CODE", "AREA_NAME", "geometry"]
            body = {
                "fields": [{"id": f} for f in fields],
                "records": [[w[f] for f in fields] for w in WARD_ROWS],
            }
        else:
            year = int(url.rsplit("sr", 1)[1][:4])
            assert year in (2025, 2026), url
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w") as z:
                z.writestr(f"SR{year}.csv", self.files[year])
            sink.write(out.getvalue())
            return b""
        return json.dumps(body).encode()


@pytest.mark.integration
def test_refresh_and_street_summary(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    monkeypatch.setattr(neighbourhood, "WARD_COUNT", 2)
    url = settings.database_url
    files = {2025: requests_csv([("2025-03-01", "Davenport (09)", "Graffiti")]),
             2026: requests_csv(ROWS)}  # fmt: skip
    first = neighbourhood.refresh(url, FakeClient(files), NOW)
    assert first["status"] == "loaded" and first["wards"] == 2
    assert [y["year"] for y in first["years"]] == [2025, 2026]
    # Unchanged: 2025 is not fetched again and 2026's hash matches.
    assert neighbourhood.refresh(url, FakeClient(files), NOW)["status"] == "unchanged"
    with connect(url) as conn:
        assert neighbourhood.ward_at(conn, 43.67, -79.44) == {"number": 9, "name": "Davenport"}
        assert neighbourhood.ward_at(conn, 43.70, -79.30) is None
        recent = neighbourhood.ward_requests(conn, 9)
    assert recent["through"] == date(2026, 10, 31) and recent["total"] == 3
    assert recent["top"] == [
        {"type": "Road - Pot hole", "requests": 2, "city": 3},
        {"type": "Graffiti", "requests": 1, "city": 2},
    ]
    client = TestClient(create_app(Settings(url), clock=lambda: NOW))
    body = client.get("/api/street", params={"at": "43.67,-79.44"}).json()
    assert body["ward"]["name"] == "Davenport" and body["requests"]["total"] == 3
    assert body["road_work"] == [] and body["collection"] is None
