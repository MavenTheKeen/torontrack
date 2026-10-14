"""Bike Share Toronto ridership from the city's open data: every trip since FIRST_YEAR.

The city publishes one zip per year ("Bike Share Toronto Ridership Data", updated monthly
for the current year): about 7 million trips and 1 GB of CSV a year. The zip is streamed to
a temporary file and its CSVs are read row by row into summaries per dock and day, dock and
hour, and pair of docks; individual trips are not stored.

What the files contain, and how the loader handles it:

- Times are Toronto local time without an offset, so dates and hours are taken as written.
- End dock IDs are written as floats in some files ("7037.0") and integers in others.
- In the 2025 files for January to November, End_Station_Name repeats the start dock's
  name even where the end dock ID differs, so names are taken from start records only.
  How often that happens is recorded per file (end_name_copied).
- Trips under 60 seconds are false starts and are dropped. A trip with no end dock (bike
  not returned to a dock) counts as a departure only. Durations count toward a typical
  trip time between 1 minute and 3 hours.
"""

import csv
import hashlib
import io
import json
import re
import tempfile
import zipfile
from collections import defaultdict
from datetime import UTC, date, datetime

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "bike-share-toronto-ridership-data"
FILE_HOST = "opendata.toronto.ca"
FIRST_YEAR = 2024
RESOURCE = re.compile(r"^bikeshare-ridership-(\d{4})\.zip$")
MAX_ZIP_BYTES = 600 << 20
MIN_SECONDS = 60
MAX_TIMED_SECONDS = 3 * 3600
EBIKES = {"EFIT", "EFIT G5", "E-FIT", "ASTRO"}


def resources(client: FeedClient) -> dict[int, dict]:
    """{year: resource} for the yearly zips from FIRST_YEAR on."""
    body = client.fetch_bytes(
        f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}", host=STATIC_HOST
    )
    out = {}
    for r in json.loads(body)["result"]["resources"]:
        match = RESOURCE.match(r.get("name", ""))
        if match and int(match[1]) >= FIRST_YEAR:
            out[int(match[1])] = r
    if not out:
        raise FetchError("No yearly ridership files found in the catalogue")
    return out


def _station(value: str) -> str | None:
    """'7037.0' -> '7037'; '' -> None."""
    value = (value or "").strip()
    if not value:
        return None
    return value[:-2] if value.endswith(".0") else value


class Summary:
    """Running totals for one year's trips."""

    def __init__(self):
        self.days = defaultdict(lambda: [0, 0, 0, 0, 0])  # dep, arr, member, ebike, round
        self.hours = defaultdict(lambda: [0, 0])  # departures, arrivals
        self.pairs = defaultdict(lambda: [0, 0, 0, 0, 0])  # trips, member, ebike, timed, secs
        self.names: dict[str, tuple[date, str]] = {}
        self.skipped = defaultdict(int)
        self.trips = 0
        self.end_name_copied = 0
        self.first = self.last = None
        self._dates: dict[str, date | None] = {}

    def _when(self, text) -> tuple[date, int] | None:
        """'2026-01-01 00:04:34' -> (date, hour). strptime on 14 million timestamps a
        year is slow, so dates are parsed once each and cached."""
        if not isinstance(text, str) or len(text) < 13 or text[10] != " ":
            return None
        key = text[:10]
        if key not in self._dates:
            try:
                self._dates[key] = date.fromisoformat(key)
            except ValueError:
                self._dates[key] = None
        day = self._dates[key]
        hour = text[11:13]
        if day is None or not hour.isdigit() or int(hour) > 23:
            return None
        return day, int(hour)

    def add(self, row: dict) -> None:
        start = _station(row.get("Start_Station_Id"))
        when = self._when(row.get("Start_Time"))
        if when is None:
            self.skipped["bad_start_time"] += 1
            return
        day, start_hour = when
        if start is None:
            self.skipped["no_start_station"] += 1
            return
        try:
            seconds = int(float(row["Trip_Duration"]))
        except (KeyError, ValueError, TypeError):
            self.skipped["bad_duration"] += 1
            return
        if seconds < MIN_SECONDS:
            self.skipped["under_60_seconds"] += 1
            return
        end = _station(row.get("End_Station_Id"))
        member = (row.get("User_Type") or "").strip() == "Member"
        ebike = (row.get("Bike_Model") or "").strip().upper() in EBIKES
        self.trips += 1
        self.first = day if self.first is None or day < self.first else self.first
        self.last = day if self.last is None or day > self.last else self.last
        month = day.replace(day=1)
        day_type = "weekend" if day.weekday() >= 5 else "weekday"
        d = self.days[(day, start)]
        d[0] += 1
        d[2] += member
        d[3] += ebike
        self.hours[(month, start, day_type, start_hour)][0] += 1
        name = (row.get("Start_Station_Name") or "").strip()
        if name and (start not in self.names or self.names[start][0] <= day):
            self.names[start] = (day, name)
        if end is None:
            self.skipped["no_end_station"] += 1  # Kept as a departure.
            return
        if end == start:
            d[4] += 1
        elif name and (row.get("End_Station_Name") or "").strip() == name:
            self.end_name_copied += 1
        self.days[(day, end)][1] += 1
        end_day, end_hour = self._when(row.get("End_Time")) or (day, start_hour)
        end_type = "weekend" if end_day.weekday() >= 5 else "weekday"
        self.hours[(end_day.replace(day=1), end, end_type, end_hour)][1] += 1
        p = self.pairs[(start, end)]
        p[0] += 1
        p[1] += member
        p[2] += ebike
        if seconds <= MAX_TIMED_SECONDS:
            p[3] += 1
            p[4] += seconds


def summarize(zip_file) -> Summary:
    """Summaries of every CSV in the zip (a path or a seekable binary file)."""
    summary = Summary()
    with zipfile.ZipFile(zip_file) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".csv"):
                continue
            with archive.open(info) as handle:
                text = io.TextIOWrapper(handle, encoding="utf-8-sig", errors="replace", newline="")
                for row in csv.DictReader(text):
                    summary.add(row)
    if not summary.trips:
        raise FetchError("Ridership file has no usable trips")
    return summary


def load(conn, year: int, resource: dict, digest: str, summary: Summary, now: datetime):
    """Replace one year's summaries in a single transaction."""
    first, last = date(year, 1, 1), date(year, 12, 31)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "DELETE FROM normalized.bikeshare_trip_station_days WHERE trip_date BETWEEN %s AND %s",
            (first, last),
        )
        cur.execute(
            "DELETE FROM normalized.bikeshare_trip_station_hours WHERE month BETWEEN %s AND %s",
            (first, last),
        )
        cur.execute("DELETE FROM normalized.bikeshare_trip_pairs WHERE year = %s", (year,))
        with cur.copy(
            """COPY normalized.bikeshare_trip_station_days (trip_date, station_id, departures,
               arrivals, member_departures, ebike_departures, round_trips) FROM STDIN"""
        ) as copy:
            for (day, station), v in summary.days.items():
                if day.year == year:
                    copy.write_row((day, station, *v))
        with cur.copy(
            """COPY normalized.bikeshare_trip_station_hours (month, station_id, day_type,
               hour, departures, arrivals) FROM STDIN"""
        ) as copy:
            for key, v in summary.hours.items():
                if key[0].year == year:
                    copy.write_row((*key, *v))
        with cur.copy(
            """COPY normalized.bikeshare_trip_pairs (year, start_station_id, end_station_id,
               trips, member_trips, ebike_trips, timed_trips, timed_seconds) FROM STDIN"""
        ) as copy:
            for (start, end), v in summary.pairs.items():
                copy.write_row((year, start, end, *v))
        cur.executemany(
            """INSERT INTO normalized.bikeshare_trip_stations (station_id, name, last_trip)
               VALUES (%s, %s, %s) ON CONFLICT (station_id) DO UPDATE
               SET name = EXCLUDED.name, last_trip = EXCLUDED.last_trip
               WHERE EXCLUDED.last_trip >= normalized.bikeshare_trip_stations.last_trip""",
            [(station, name, day) for station, (day, name) in summary.names.items()],
        )
        cur.execute(
            """INSERT INTO normalized.bikeshare_trip_files VALUES
               (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (year) DO UPDATE SET resource_name = EXCLUDED.resource_name,
                   url = EXCLUDED.url, source_modified = EXCLUDED.source_modified,
                   file_sha256 = EXCLUDED.file_sha256, loaded_at = EXCLUDED.loaded_at,
                   trips = EXCLUDED.trips, first_trip = EXCLUDED.first_trip,
                   last_trip = EXCLUDED.last_trip, skipped = EXCLUDED.skipped,
                   end_name_copied = EXCLUDED.end_name_copied""",
            (
                year,
                resource["name"],
                resource["url"],
                resource.get("last_modified"),
                digest,
                now,
                summary.trips,
                summary.first,
                summary.last,
                json.dumps(dict(summary.skipped)),
                summary.end_name_copied,
            ),
        )


def refresh(database_url: str, client: FeedClient, years: list[int] | None = None) -> dict:
    """Load every yearly file whose catalogue entry or contents changed since its last
    load (or the given years regardless)."""
    started = datetime.now(UTC)
    loaded, result = [], {"status": "failed"}
    with connect(database_url) as conn:
        try:
            current = {
                r["year"]: r for r in conn.execute("SELECT * FROM normalized.bikeshare_trip_files")
            }
            for year, resource in sorted(resources(client).items()):
                known = current.get(year)
                if (
                    years is None
                    and known
                    and known["source_modified"] == resource.get("last_modified")
                ):
                    continue
                if years is not None and year not in years:
                    continue
                if not resource["url"].startswith(f"https://{FILE_HOST}/"):
                    raise FetchError(f"Ridership file for {year} is not on {FILE_HOST}")
                with tempfile.TemporaryFile() as sink:
                    client.fetch_bytes(
                        resource["url"], host=FILE_HOST, max_bytes=MAX_ZIP_BYTES, sink=sink
                    )
                    sink.seek(0)
                    digest = hashlib.file_digest(sink, "sha256").hexdigest()
                    if years is None and known and known["file_sha256"] == digest:
                        conn.execute(
                            """UPDATE normalized.bikeshare_trip_files SET source_modified = %s
                               WHERE year = %s""",
                            (resource.get("last_modified"), year),
                        )
                        continue
                    sink.seek(0)
                    summary = summarize(sink)
                load(conn, year, resource, digest, summary, started)
                loaded.append(year)
            result = {"status": "loaded" if loaded else "unchanged", "years_loaded": loaded}
        except (FetchError, zipfile.BadZipFile, ValueError, KeyError) as exc:
            result = {"status": "failed", "years_loaded": loaded, "error": str(exc)[:1000]}
        conn.execute(
            """INSERT INTO ops.bikeshare_trip_refreshes
               (started_at, finished_at, status, years_loaded, error)
               VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], loaded or None, result.get("error")),
        )
    return result
