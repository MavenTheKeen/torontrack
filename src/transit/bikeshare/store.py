"""Bike Share persistence; source writes and normalized writes have separate commits.

Storage avoids repeating what does not change (see migration 0001): a payload's "data"
member is stored once per distinct content, station metadata once per version, and
per-collection values once per collection."""

import hashlib
import json
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from transit import locks
from transit.bikeshare.parsing import (
    FeedValidationError,
    parse_information,
    parse_status,
    timestamp,
)
from transit.config import FUTURE_TOLERANCE_SECONDS, STALE_SECONDS

LOCK_ID = locks.BIKESHARE_COLLECTION  # Session lock spans HTTP and SQL.


def payload_hash(payload):
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def store_raw(conn, collection_id, feed_name, url, payload):
    # Record even a JSON object that fails GBFS validation. Publication may be unknown.
    try:
        published_at = timestamp(payload.get("last_updated"))
    except FeedValidationError:
        published_at = None
    fetched_at = datetime.now(UTC)
    has_body = isinstance(payload, dict) and "data" in payload
    envelope = {k: v for k, v in payload.items() if k != "data"} if has_body else payload
    with conn.transaction():
        body_hash = None
        if has_body:
            # PostgreSQL hashes the canonical jsonb text, so identical bodies always match.
            body_hash = conn.execute(
                """WITH body AS (SELECT raw.body_hash(%(body)s::jsonb) AS body_hash,
                                        %(body)s::jsonb AS body),
                        stored AS (INSERT INTO raw.payload_bodies
                                   SELECT body_hash, body FROM body
                                   ON CONFLICT (body_hash) DO NOTHING)
                   SELECT body_hash FROM body""",
                {"body": Jsonb(payload["data"])},
            ).fetchone()["body_hash"]
        result = conn.execute(
            """INSERT INTO raw.feed_payloads
           (collection_id, feed_name, feed_url, fetched_at, source_published_at,
            payload_hash, envelope, body_hash)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (collection_id, feed_name) DO NOTHING RETURNING raw_id""",
            (
                collection_id,
                feed_name,
                url,
                fetched_at,
                published_at,
                payload_hash(payload),
                Jsonb(envelope),
                body_hash,
            ),
        ).fetchone()
        if feed_name == "station_status" and result:
            # Actual status acquisition, never the Airflow logical date or original
            # failed attempt's start. A retry tomorrow cannot invent yesterday's data.
            conn.execute(
                "UPDATE ops.ingestion_runs SET collected_at=%s WHERE collection_id=%s",
                (fetched_at, collection_id),
            )
    return result


def load_raw(conn, collection_id):
    return {
        r["feed_name"]: r
        for r in conn.execute(
            "SELECT * FROM raw.feed_payloads_full WHERE collection_id = %s", (collection_id,)
        ).fetchall()
    }


def age_valid(observed_at, reported_at):
    age = (observed_at - reported_at).total_seconds()
    return -FUTURE_TOLERANCE_SECONDS <= age <= STALE_SECONDS


def station_versions(conn, stations) -> dict[str, int]:
    """Map each station_id to the version row for its current name, location and
    capacity, adding a row only when one of those values is new."""
    columns = [
        [s["station_id"] for s in stations],
        [s["name"] for s in stations],
        [s["lat"] for s in stations],
        [s["lon"] for s in stations],
        [s["capacity"] for s in stations],
    ]
    match = """
        SELECT v.station_id, v.station_version_id
        FROM unnest(%s::text[], %s::text[], %s::float8[], %s::float8[], %s::integer[])
             AS s(station_id, name, lat, lon, capacity)
        JOIN normalized.station_versions v
          ON v.station_id = s.station_id AND v.name = s.name AND v.lat = s.lat
         AND v.lon = s.lon AND v.capacity IS NOT DISTINCT FROM s.capacity"""
    found = {r["station_id"]: r["station_version_id"] for r in conn.execute(match, columns)}
    if len(found) < len(stations):
        conn.execute(
            """INSERT INTO normalized.station_versions (station_id, name, lat, lon, capacity)
               SELECT * FROM unnest(%s::text[], %s::text[], %s::float8[], %s::float8[],
                                    %s::integer[])
               ON CONFLICT DO NOTHING""",
            columns,
        )
        found = {r["station_id"]: r["station_version_id"] for r in conn.execute(match, columns)}
    return found


def normalize(conn, collection_id):
    raw = load_raw(conn, collection_id)
    if not {"station_information", "station_status"} <= raw.keys():
        raise FeedValidationError(
            "Replay needs both stored station feeds; collect can fetch missing feeds"
        )
    information = raw["station_information"]
    status = raw["station_status"]
    stations = parse_information(information["payload"])
    observations = parse_status(status["payload"])
    station_ids = {r["station_id"] for r in stations}
    observed_ids = {r["station_id"] for r in observations}
    unknown = observed_ids - station_ids
    if unknown:
        raise FeedValidationError(f"Status has {len(unknown)} stations absent from metadata")
    collected_at = conn.execute(
        "SELECT collected_at FROM ops.ingestion_runs WHERE collection_id=%s", (collection_id,)
    ).fetchone()["collected_at"]
    stale_count = sum(
        not age_valid(status["fetched_at"], row["station_reported_at"]) for row in observations
    )
    checks = [
        ("required_fields_and_unique_stations", True, {"stations": len(stations)}),
        (
            "station_status_coverage",
            station_ids == observed_ids,
            {
                "metadata_stations": len(station_ids),
                "observed_stations": len(observed_ids),
                "missing_status": len(station_ids - observed_ids),
            },
        ),
        (
            "feed_freshness",
            all(
                age_valid(r["fetched_at"], r["source_published_at"]) for r in (information, status)
            ),
            {"threshold_seconds": STALE_SECONDS},
        ),
        (
            "station_report_freshness",
            stale_count == 0,
            {
                "stale_or_future_reports": stale_count,
                "observed_stations": len(observations),
                "threshold_seconds": STALE_SECONDS,
            },
        ),
    ]
    # Replay replaces only this collection atomically. A failed insert leaves old data intact.
    with conn.transaction():
        collection_key = conn.execute(
            """INSERT INTO normalized.collections
               (collection_id, collected_at, status_fetched_at, status_published_at,
                information_raw_id, status_raw_id)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (collection_id) DO UPDATE SET
                 collected_at = excluded.collected_at,
                 status_fetched_at = excluded.status_fetched_at,
                 status_published_at = excluded.status_published_at,
                 information_raw_id = excluded.information_raw_id,
                 status_raw_id = excluded.status_raw_id
               RETURNING collection_key""",
            (
                collection_id,
                collected_at,
                status["fetched_at"],
                status["source_published_at"],
                information["raw_id"],
                status["raw_id"],
            ),
        ).fetchone()["collection_key"]
        conn.execute(
            "DELETE FROM normalized.observations WHERE collection_key = %s", (collection_key,)
        )
        conn.execute(
            "DELETE FROM normalized.unobserved_stations WHERE collection_key = %s",
            (collection_key,),
        )
        versions = station_versions(conn, stations)
        reported = {r["station_id"] for r in observations}
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO normalized.observations
                   (collection_key, station_version_id, station_reported_at,
                    num_bikes_available, num_docks_available, num_bikes_disabled,
                    num_docks_disabled, is_installed, is_renting, is_returning, station_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    (
                        collection_key,
                        versions[r["station_id"]],
                        r["station_reported_at"],
                        r["num_bikes_available"],
                        r["num_docks_available"],
                        r["num_bikes_disabled"],
                        r["num_docks_disabled"],
                        r["is_installed"],
                        r["is_renting"],
                        r["is_returning"],
                        r["station_id"],
                    )
                    for r in observations
                ],
            )
            # Listed in metadata but absent from status: kept so coverage stays exact.
            cur.executemany(
                """INSERT INTO normalized.unobserved_stations
                   (collection_key, station_version_id, station_id) VALUES (%s,%s,%s)""",
                [
                    (collection_key, versions[s["station_id"]], s["station_id"])
                    for s in stations
                    if s["station_id"] not in reported
                ],
            )
            cur.executemany(
                """INSERT INTO ops.quality_checks (collection_id,check_name,passed,details)
                   VALUES (%s,%s,%s,%s) ON CONFLICT (collection_id,check_name)
                   DO UPDATE SET passed=excluded.passed,details=excluded.details""",
                [(collection_id, name, passed, Jsonb(details)) for name, passed, details in checks],
            )
        conn.execute(
            """UPDATE ops.ingestion_runs SET status='succeeded', completed_at=now(), error=NULL
               WHERE collection_id=%s""",
            (collection_id,),
        )
    return len(observations)
