"""Bike Share ridership files: summarizing trips, the files' quirks, loading and the API."""

import io
import json
import zipfile
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from transit.bikeshare import trips
from transit.db import connect
from transit.web.app import create_app

HEADER = (
    "Trip_Id,Trip_Duration,Start_Station_Id,Start_Time,Start_Station_Name,End_Station_Id,"
    "End_Time,End_Station_Name,Bike_Id,User_Type,Bike_Model\r\n"
)
ROWS = [
    # A member's e-bike trip, 8 a.m. on a Friday.
    "1,600,7001,2025-07-04 08:05:00,King St / Bay St,7002.0,2025-07-04 08:15:00,"
    "Queen St / Yonge St,11,Member,EFIT",
    # The 2025 quirk: the end name repeats the start name though the dock differs.
    "2,900,7001,2025-07-04 08:30:00,King St / Bay St,7002,2025-07-04 08:45:00,"
    "King St / Bay St,12,Casual,ICONIC",
    # A round trip on a Saturday.
    "3,1200,7002,2025-07-05 17:00:00,Queen St / Yonge St,7002,2025-07-05 17:20:00,"
    "Queen St / Yonge St,13,Casual,ICONIC",
    # Not returned to a dock: a departure only.
    "4,700,7001,2025-07-05 09:00:00,King St / Bay St,,2025-07-05 09:11:40,,14,Member,ICONIC",
    # A false start, under a minute.
    "5,20,7001,2025-07-05 09:30:00,King St / Bay St,7001,2025-07-05 09:30:20,"
    "King St / Bay St,15,Member,ICONIC",
    # Unreadable start time.
    "6,300,7001,not a time,King St / Bay St,7002,2025-07-05 10:05:00,Queen St,16,Member,ICONIC",
]


def ridership_zip(rows=ROWS, name="bikeshare_2025_07.csv") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, "﻿" + HEADER + "\r\n".join(rows) + "\r\n")
    return out.getvalue()


def test_summary_counts_trips_and_records_the_files_quirks():
    s = trips.summarize(io.BytesIO(ridership_zip()))
    assert s.trips == 4
    assert dict(s.skipped) == {"no_end_station": 1, "under_60_seconds": 1, "bad_start_time": 1}
    assert s.end_name_copied == 1
    friday = date(2025, 7, 4)
    # departures, arrivals, member departures, e-bike departures, round trips
    assert s.days[(friday, "7001")] == [2, 0, 1, 1, 0]
    assert s.days[(friday, "7002")] == [0, 2, 0, 0, 0]
    assert s.days[(date(2025, 7, 5), "7002")] == [1, 1, 0, 0, 1]
    assert s.hours[(date(2025, 7, 1), "7001", "weekday", 8)] == [2, 0]
    assert s.hours[(date(2025, 7, 1), "7002", "weekend", 17)] == [1, 1]
    # trips, member, e-bike, timed trips, timed seconds; "7002.0" is dock 7002.
    assert s.pairs[("7001", "7002")] == [2, 1, 1, 2, 1500]
    assert s.names["7001"][1] == "King St / Bay St"


class Client:
    def __init__(self, files: dict[int, bytes], modified="2026-10-01"):
        self.files, self.modified, self.calls = files, modified, []

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None):
        self.calls.append(url)
        if "package_show" in url:
            return json.dumps(
                {
                    "result": {
                        "resources": [
                            {
                                "name": f"bikeshare-ridership-{year}.zip",
                                "url": f"https://{trips.FILE_HOST}/x/{year}.zip",
                                "last_modified": self.modified,
                            }
                            for year in self.files
                        ]
                        + [{"name": "bikeshare-ridership-2016.xlsx", "url": "https://x/old"}]
                    }
                }
            ).encode()
        assert host == trips.FILE_HOST
        sink.write(self.files[int(url.rsplit("/", 1)[1][:4])])
        return b""


@pytest.mark.integration
def test_refresh_loads_changed_years_and_serves_a_docks_trips(settings):
    client = Client({2025: ridership_zip()})
    assert trips.refresh(settings.database_url, client)["years_loaded"] == [2025]
    # Same catalogue entry: no download at all.
    calls = len(client.calls)
    assert trips.refresh(settings.database_url, client)["status"] == "unchanged"
    assert len(client.calls) == calls + 1
    with connect(settings.database_url) as conn:
        [row] = conn.execute("SELECT * FROM normalized.bikeshare_trip_files").fetchall()
        assert (row["trips"], row["end_name_copied"], str(row["last_trip"])) == (
            4,
            1,
            "2025-07-05",
        )
    app = create_app(settings.database_url, clock=lambda: datetime(2025, 8, 1, tzinfo=UTC))
    body = TestClient(app).get("/api/docks/7001/trips").json()
    assert body["totals"]["departures"] == 3 and body["totals"]["member_departures"] == 2
    [top] = body["destinations"]
    assert (top["station_id"], top["trips"], top["typical_minutes"]) == (
        "7002",
        2,
        13,
    )  # 750 s: 12.5 min rounds up
    assert TestClient(app).get("/api/docks/9999/trips").status_code == 404
