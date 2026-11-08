"""City of Toronto food premises inspections (DineSafe), for the map and place search.

The city publishes every inspection of the last two to three years as one table, refreshed
daily: one row per infraction found, or one row for an inspection with none. Each premises
keeps its city ID; an inspection is identified by premises and date. Inspections are kept
after they leave the city's file, so the history grows past its window.

Contains information licensed under the Open Government Licence – Toronto.
"""

import csv
import hashlib
import io
import json
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "dinesafe"
MAX_BYTES = 200 << 20
BOUNDS = (43.5, 43.9, -79.7, -79.05)
# The file has had about 20,000 premises; far fewer means a truncated or broken export.
MIN_PREMISES = 5000
SEVERITIES = {
    "M - Minor": "minor",
    "S - Significant": "significant",
    "C - Crucial": "crucial",
    "NA": "not applicable",
}
OUTCOMES = {"Conviction: Fined": "Conviction - Fined"}


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return None if value in ("", "None") else value


def _address(value: str) -> str:
    # Empty address parts are exported as the word None, e.g. "1871 O'Connor Dr None M4A 1X1".
    return " ".join(part for part in value.split() if part != "None")


def _fine(value: str | None) -> Decimal | None:
    value = _blank(value)
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Fine {value!r} is not a number") from exc


def read(feed) -> tuple[dict, dict, list, int]:
    """Premises, inspections and infractions from the city's CSV export (a text stream).
    Rows outside the city or without a premises, date or status are skipped and counted."""
    premises, inspections, infractions = {}, {}, []
    skipped = 0
    south, north, west, east = BOUNDS
    for row in csv.DictReader(feed):
        try:
            pid = _blank(row["estId"])
            inspected = date.fromisoformat(row["inspectionDate"])
            status = _blank(row["inspectionStatus"])
            lat, lon = float(row["latitude"]), float(row["longitude"])
        except (KeyError, ValueError):
            skipped += 1
            continue
        if not pid or not status or not (south <= lat <= north and west <= lon <= east):
            skipped += 1
            continue
        premises[pid] = {
            "premises_id": pid,
            "name": row["estName"].strip(),
            "address": _address(row["address"]),
            "lat": lat,
            "lon": lon,
        }
        inspections.setdefault((pid, inspected), status)
        infraction = _blank(row["typeDesc"])
        if infraction:
            outcome = _blank(row["OutcomeDesc"])
            outcome_on = _blank(row["OutcomeDate"])
            infractions.append(
                (
                    row["unique_id"],
                    pid,
                    inspected,
                    infraction,
                    _blank(row["deficiencyDesc"]),
                    SEVERITIES.get(row["severity"]),
                    OUTCOMES.get(outcome, outcome),
                    date.fromisoformat(outcome_on[:10]) if outcome_on else None,
                    _fine(row["amountFined"]),
                )
            )
    return premises, inspections, infractions, skipped


def load(conn, premises: dict, inspections: dict, infractions: list, now: datetime) -> None:
    """Upsert premises and inspections, and replace the infractions of every inspection in
    the file, in one transaction."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE premises_in (LIKE normalized.food_premises) ON COMMIT DROP"""
        )
        with cur.copy(
            "COPY premises_in (premises_id, name, address, lat, lon, last_seen_at) FROM STDIN"
        ) as copy:
            for p in premises.values():
                copy.write_row((p["premises_id"], p["name"], p["address"], p["lat"], p["lon"], now))
        cur.execute(
            """INSERT INTO normalized.food_premises SELECT * FROM premises_in
               ON CONFLICT (premises_id) DO UPDATE SET name = EXCLUDED.name,
                 address = EXCLUDED.address, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                 last_seen_at = EXCLUDED.last_seen_at"""
        )
        cur.execute(
            """CREATE TEMP TABLE inspections_in (LIKE normalized.food_inspections)
               ON COMMIT DROP"""
        )
        with cur.copy("COPY inspections_in (premises_id, inspected_on, status) FROM STDIN") as copy:
            for (pid, inspected), status in inspections.items():
                copy.write_row((pid, inspected, status))
        cur.execute(
            """INSERT INTO normalized.food_inspections SELECT * FROM inspections_in
               ON CONFLICT (premises_id, inspected_on) DO UPDATE SET status = EXCLUDED.status"""
        )
        cur.execute(
            """DELETE FROM normalized.food_infractions f USING inspections_in i
               WHERE f.premises_id = i.premises_id AND f.inspected_on = i.inspected_on"""
        )
        with cur.copy(
            """COPY normalized.food_infractions (infraction_id, premises_id, inspected_on,
                   infraction, category, severity, outcome, outcome_on, fine) FROM STDIN"""
        ) as copy:
            for row in infractions:
                copy.write_row(row)


def _resource_url(meta: dict) -> str:
    """The table export of the datastore copy, which carries the newest inspections."""
    for resource in meta["result"]["resources"]:
        if resource.get("datastore_active"):
            return f"https://{STATIC_HOST}/datastore/dump/{resource['id']}"
    raise FetchError("DineSafe datastore resource not found")


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed", "feed_sha256": None}
    with connect(database_url) as conn:
        try:
            meta = json.loads(
                client.fetch_bytes(
                    f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}",
                    host=STATIC_HOST,
                )
            )
            with tempfile.TemporaryFile() as feed:
                client.fetch_bytes(
                    _resource_url(meta), host=STATIC_HOST, max_bytes=MAX_BYTES, sink=feed
                )
                feed.seek(0)
                digest = hashlib.file_digest(feed, "sha256").hexdigest()
                result["feed_sha256"] = digest
                last = conn.execute(
                    """SELECT feed_sha256 FROM ops.food_inspection_refreshes
                       WHERE status = 'loaded' ORDER BY refresh_id DESC LIMIT 1"""
                ).fetchone()
                if last and last["feed_sha256"] == digest:
                    result["status"] = "unchanged"
                else:
                    feed.seek(0)
                    text = io.TextIOWrapper(feed, encoding="utf-8-sig", newline="")
                    premises, inspections, infractions, skipped = read(text)
                    text.detach()  # Leave the file to the with block.
                    if len(premises) < MIN_PREMISES:
                        raise FetchError(
                            f"DineSafe export lists only {len(premises)} premises; not loaded"
                        )
                    load(conn, premises, inspections, infractions, started)
                    result.update(
                        status="loaded",
                        premises=len(premises),
                        inspections=len(inspections),
                        infractions=len(infractions),
                        skipped=skipped,
                    )
        except (FetchError, KeyError, ValueError, csv.Error) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.food_inspection_refreshes (started_at, finished_at, status,
                   feed_sha256, premises, inspections, infractions, skipped, error)
               VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["feed_sha256"],
                result.get("premises"),
                result.get("inspections"),
                result.get("infractions"),
                result.get("skipped"),
                result.get("error"),
            ),
        )
    return result
