"""Open-data files: daily Parquet and gzipped CSV, each set with a JSON sidecar that
records row count, time range, checksums and column descriptions. index.json lists
every published set; the public site serves the folder at /data/.

A file set is written to a temporary name and renamed into place, sidecar last, so a
sidecar's presence means its files are complete. Files are regenerated, never edited."""

import hashlib
import json
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from transit.db import connect
from transit.ttc.realtime import TORONTO

LICENCE = (
    "Contains information licensed under the Open Government Licence - Toronto. "
    "Sources: Toronto Transit Commission; Bike Share Toronto / Toronto Parking Authority. "
    "Derived by Torontrack; not endorsed by either."
)
LICENCE_URL = (
    "https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/"
)
# A day is exported once it is over: the TTC service day ends at 04:00 Toronto time.
DAY_END = time(4)

DAILY = {
    "ttc-headways": {
        "title": "TTC subway headways",
        "description": (
            "Every observed gap between consecutive trains at a subway platform. Arrivals "
            "are inferred from TTC's realtime predictions (see docs/ttc.md); rows flagged "
            "implausible, spanning a collection gap or outside scheduled service are kept "
            "but marked."
        ),
        "day": "TTC service day: 04:00 to 04:00 Toronto time",
        "query": """
            SELECT service_date, service_hour, route_id, direction_id, towards, stop_id,
                   station_key, station_name, platform, is_terminal, train_id,
                   previous_train_id, previous_arrival_at, arrived_at, headway_seconds,
                   implausible, spans_collection_gap, outside_service
            FROM analytics.ttc_headways WHERE service_date = %(day)s
            ORDER BY arrived_at, stop_id""",
        "time_column": "arrived_at",
        "columns": {
            "service_date": "TTC service day (trips after midnight belong to the previous day)",
            "service_hour": "Hour of the service day in Toronto time; 24+ is after midnight",
            "route_id": "Subway line: 1, 2 or 4",
            "direction_id": "GTFS direction (0 or 1); see towards",
            "towards": "Terminal station the train was heading to",
            "stop_id": "GTFS platform ID",
            "station_key": "Station identifier used by this project",
            "station_name": "Station name",
            "platform": "Platform direction label from GTFS",
            "is_terminal": "Platform is a line terminal (dwell times inflate headways)",
            "train_id": "Train (vehicle label) whose arrival ends the gap",
            "previous_train_id": "Train whose arrival starts the gap",
            "previous_arrival_at": "Inferred arrival of the previous train, UTC",
            "arrived_at": "Inferred arrival of this train, UTC",
            "headway_seconds": "Seconds between the two arrivals",
            "implausible": "Under 60 s: usually one train reported under two labels",
            "spans_collection_gap": "The collector was down during the gap; not a delay",
            "outside_service": (
                "A train passed outside scheduled service (e.g. a work train before "
                "opening); the gap spans the overnight closure, not a delay"
            ),
        },
    },
    "bikeshare-availability": {
        "title": "Bike Share Toronto dock availability",
        "description": (
            "Every station listed in each 15-minute collection, with its reported "
            "availability. observed is false when the station was listed without a status "
            "report. A reading is current only if station_reported_at is within 30 minutes "
            "of collected_at (see docs/metrics.md)."
        ),
        "day": "Calendar day in Toronto time, by collected_at",
        "query": """
            SELECT collection_id, collected_at, station_id, name, lat, lon, capacity,
                   observed, station_reported_at, num_bikes_available, num_docks_available,
                   num_bikes_disabled, num_docks_disabled, is_installed, is_renting,
                   is_returning
            FROM normalized.station_observations
            WHERE collected_at >= %(start)s AND collected_at < %(end)s
            ORDER BY collected_at, station_id""",
        "time_column": "collected_at",
        "columns": {
            "collection_id": "Identifier of the 15-minute collection",
            "collected_at": "When this collection's status was fetched, UTC",
            "station_id": "Bike Share station ID",
            "name": "Station name at that time",
            "lat": "Latitude",
            "lon": "Longitude",
            "capacity": "Docks at the station, if published",
            "observed": "The station had a status report in this collection",
            "station_reported_at": "When the station itself last reported, UTC",
            "num_bikes_available": "Bikes available to rent",
            "num_docks_available": "Empty docks available for returns",
            "num_bikes_disabled": "Bikes out of service, if published",
            "num_docks_disabled": "Docks out of service, if published",
            "is_installed": "Station installed",
            "is_renting": "Station allowing rentals",
            "is_returning": "Station accepting returns",
        },
    },
}
SNAPSHOTS = {
    "ttc-elevator-outages": {
        "title": "TTC elevator and escalator outages",
        "description": (
            "Every elevator or escalator outage TTC reported since collection began, with "
            "its duration. Regenerated daily; ongoing outages have no resolved_at."
        ),
        "query": """
            SELECT alert_id, station_key, station_name, device_type, header_text,
                   first_seen_at, resolved_at, active, duration_minutes,
                   began_before_collection
            FROM analytics.ttc_elevator_outages ORDER BY first_seen_at, alert_id""",
        "columns": {
            "alert_id": "TTC alert ID",
            "station_key": "Station identifier used by this project",
            "station_name": "Station name",
            "device_type": "elevator or escalator",
            "header_text": "TTC's alert text",
            "first_seen_at": "First time the outage appeared in the feed, UTC",
            "resolved_at": "First time it no longer appeared, UTC; empty while ongoing",
            "active": "Still out of service at export time",
            "duration_minutes": "Minutes out of service so far",
            "began_before_collection": "Already out when collection began: a lower bound",
        },
    },
}


def service_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Toronto calendar-day bounds in UTC, correct across daylight-saving changes."""
    start = datetime.combine(day, time(0), TORONTO)
    return start.astimezone(UTC), datetime.combine(day + timedelta(1), time(0), TORONTO).astimezone(
        UTC
    )


def last_complete_day(now: datetime) -> date:
    local = now.astimezone(TORONTO)
    return local.date() - timedelta(days=1 if local.time() >= DAY_END else 2)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_set(folder: Path, stem: str, rows: list[dict], meta: dict) -> dict:
    """Write stem.parquet, stem.csv.gz and stem.json into folder; return the sidecar."""
    folder.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    files = {}
    for fmt, name in (("parquet", f"{stem}.parquet"), ("csv", f"{stem}.csv.gz")):
        final, partial = folder / name, folder / f".{name}.partial"
        if fmt == "parquet":
            pq.write_table(table, partial, compression="zstd")
        else:
            with pa.CompressedOutputStream(str(partial), "gzip") as out:
                pacsv.write_csv(table, out)
        os.replace(partial, final)
        files[fmt] = {
            "path": f"{folder.name}/{name}",
            "bytes": final.stat().st_size,
            "sha256": _sha256(final),
        }
    sidecar = {
        **meta,
        "rows": len(rows),
        "files": files,
        "licence": LICENCE,
        "licence_url": LICENCE_URL,
    }
    partial = folder / f".{stem}.json.partial"
    partial.write_text(json.dumps(sidecar, indent=2, default=str), encoding="utf-8")
    os.replace(partial, folder / f"{stem}.json")
    return sidecar


# The table each dataset reads first; dbt-built ones are absent until the first build.
SOURCES = {
    "ttc-headways": "analytics.ttc_headways",
    "bikeshare-availability": "normalized.collections",
    "ttc-elevator-outages": "analytics.ttc_elevator_outages",
}


def _exists(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) AS r", (SOURCES[name],)).fetchone()["r"] is not None


def _first_day(conn, name: str) -> date | None:
    if name == "ttc-headways":
        row = conn.execute("SELECT min(service_date) AS d FROM analytics.ttc_headways")
        return row.fetchone()["d"]
    first = conn.execute("SELECT min(collected_at) AS t FROM normalized.collections")
    first = first.fetchone()["t"]
    return first.astimezone(TORONTO).date() if first else None


def export(
    database_url: str,
    out_dir: str | Path,
    now: datetime,
    days: list[date] | None = None,
    force: bool = False,
) -> dict:
    """Write each completed day not yet exported (or the given days), refresh the
    snapshot datasets, and rebuild index.json. Returns what was written."""
    out = Path(out_dir)
    written = {}
    # One read-only snapshot, so every file reflects the same moment.
    with connect(database_url) as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        last = last_complete_day(now)
        for name, spec in DAILY.items():
            first = _first_day(conn, name) if _exists(conn, name) else None
            if first is None:
                continue
            wanted = days or [first + timedelta(n) for n in range((last - first).days + 1)]
            for day in wanted:
                if day > last or day < first:
                    continue
                if not force and (out / name / f"{day}.json").exists():
                    continue
                start, end = service_day_bounds(day)
                rows = conn.execute(
                    spec["query"], {"day": day, "start": start, "end": end}
                ).fetchall()
                if not rows:
                    continue
                times = [r[spec["time_column"]] for r in rows]
                meta = {
                    "dataset": name,
                    "day": str(day),
                    "first_record_at": min(times),
                    "last_record_at": max(times),
                    "partial": day == first,
                    "generated_at": now,
                }
                _write_set(out / name, str(day), rows, meta)
                written.setdefault(name, []).append(str(day))
        for name, spec in SNAPSHOTS.items():
            rows = conn.execute(spec["query"]).fetchall() if _exists(conn, name) else []
            if rows:
                _write_set(
                    out / name, "latest", rows, {"dataset": name, "day": None, "generated_at": now}
                )
                written.setdefault(name, []).append("latest")
    write_index(out, now)
    return written


def write_index(out: Path, now: datetime) -> dict:
    datasets = []
    for name, spec in {**DAILY, **SNAPSHOTS}.items():
        sets = (
            sorted(
                (json.loads(p.read_text(encoding="utf-8")) for p in (out / name).glob("*.json")),
                key=lambda s: s.get("day") or "",
                reverse=True,
            )
            if (out / name).is_dir()
            else []
        )
        datasets.append(
            {
                "name": name,
                "title": spec["title"],
                "description": spec["description"],
                "day": spec.get("day"),
                "columns": spec["columns"],
                "files": [
                    {k: s[k] for k in ("day", "rows", "partial", "files") if k in s} for s in sets
                ],
            }
        )
    index = {
        "generated_at": now,
        "licence": LICENCE,
        "licence_url": LICENCE_URL,
        "datasets": datasets,
    }
    out.mkdir(parents=True, exist_ok=True)
    partial = out / ".index.json.partial"
    partial.write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")
    os.replace(partial, out / "index.json")
    return index
