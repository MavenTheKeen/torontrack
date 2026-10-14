"""Schema migrations run once, in order, and keep every existing value."""

import json
import os

import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from transit.db import connect, init_db, migrations

pytestmark = pytest.mark.integration
DATABASE = "transit_migration_test"  # Disposable; created and dropped by this module.

T1, T2 = "2026-10-09 13:00:00+00", "2026-10-09 13:15:00+00"


@pytest.fixture
def old_database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration checks")
    admin = make_conninfo(url, dbname="postgres")
    with connect(admin) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(DATABASE)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DATABASE)))
    target = make_conninfo(url, dbname=DATABASE)
    init_db(target, upto="0000_baseline_ttc")  # The schema before any migration.
    yield target
    with connect(admin) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DATABASE))
        )


def payload(updated, stations):
    return {"last_updated": updated, "ttl": 60, "data": {"stations": stations}}


INFO = [{"station_id": "1", "name": "A"}, {"station_id": "2", "name": "B"}]


def load_old_shape(conn):
    """Two collections in the pre-migration layout: station 1 renamed in the second,
    station 3 listed only in the second collection's metadata with no status."""
    raw = {}
    for cid, at in (("c1", T1), ("c2", T2)):
        conn.execute(
            "INSERT INTO ops.ingestion_runs VALUES (%s, %s, 'succeeded', %s, NULL)", (cid, at, at)
        )
        bodies = {
            "gbfs": {"last_updated": at, "data": {"feeds": ["x"]}},
            # Same station data both times; only the envelope timestamp differs.
            "station_information": payload(at, INFO),
            "station_status": payload(at, [{"station_id": "1", "bikes": cid}]),
        }
        for feed, body in bodies.items():
            raw[(cid, feed)] = conn.execute(
                """INSERT INTO raw.feed_payloads (collection_id, feed_name, feed_url,
                       fetched_at, source_published_at, payload_hash, payload)
                   VALUES (%s, %s, 'test://', %s, %s, repeat('0', 64), %s)
                   RETURNING raw_id""",
                (cid, feed, at, at, json.dumps(body)),
            ).fetchone()["raw_id"]
    snapshots = [
        ("c1", "1", "Old name"),
        ("c1", "2", "Two"),
        ("c2", "1", "New name"),
        ("c2", "2", "Two"),
        ("c2", "3", "Metadata only"),
    ]
    for cid, station, name in snapshots:
        conn.execute(
            """INSERT INTO normalized.station_snapshots VALUES (%s, %s, %s, 43.6, -79.4,
                   CASE WHEN %s = '2' THEN NULL ELSE 15 END, %s)""",
            (cid, station, name, station, raw[(cid, "station_information")]),
        )
    for cid, at in (("c1", T1), ("c2", T2)):
        for station, bikes in (("1", 3), ("2", 0)):
            conn.execute(
                """INSERT INTO normalized.observations VALUES (%s, %s, %s, %s, %s, %s, %s, 5,
                       NULL, 1, true, true, %s, %s)""",
                (cid, station, at, at, at, at, bikes, station == "1", raw[(cid, "station_status")]),
            )


# One row per metadata station, in the shape readers saw before the migration.
OLD_ROWS = """
    SELECT s.collection_id, s.station_id, s.name, s.lat, s.lon, s.capacity,
           s.information_raw_id, o.collected_at, o.status_fetched_at, o.source_published_at,
           o.station_reported_at, o.num_bikes_available, o.num_docks_available,
           o.num_bikes_disabled, o.num_docks_disabled, o.is_installed, o.is_renting,
           o.is_returning, o.status_raw_id, o.station_id IS NOT NULL AS observed
    FROM normalized.station_snapshots s
    LEFT JOIN normalized.observations o USING (collection_id, station_id)
    ORDER BY 1, 2"""
NEW_ROWS = """
    SELECT collection_id, station_id, name, lat, lon, capacity, information_raw_id,
           -- The old layout only had these on observed rows; the view has them on all.
           CASE WHEN observed THEN collected_at END AS collected_at,
           CASE WHEN observed THEN status_fetched_at END AS status_fetched_at,
           CASE WHEN observed THEN source_published_at END AS source_published_at,
           station_reported_at, num_bikes_available, num_docks_available,
           num_bikes_disabled, num_docks_disabled, is_installed, is_renting, is_returning,
           CASE WHEN observed THEN status_raw_id END AS status_raw_id, observed
    FROM normalized.station_observations
    ORDER BY 1, 2"""


def test_compact_storage_migration_keeps_every_value(old_database):
    with connect(old_database) as conn:
        load_old_shape(conn)
        before_rows = [tuple(r.values()) for r in conn.execute(OLD_ROWS)]
        before_raw = {
            r["raw_id"]: r["payload"]
            for r in conn.execute("SELECT raw_id, payload FROM raw.feed_payloads")
        }
    applied = init_db(old_database)
    assert applied == [v for v, _ in migrations() if not v.startswith("0000")]
    with connect(old_database) as conn:
        assert [tuple(r.values()) for r in conn.execute(NEW_ROWS)] == before_rows
        after_raw = {
            r["raw_id"]: r["payload"]
            for r in conn.execute("SELECT raw_id, payload FROM raw.feed_payloads_full")
        }
        assert after_raw == before_raw
        counts = conn.execute(
            """SELECT (SELECT count(*) FROM raw.payload_bodies) AS bodies,
                      (SELECT count(*) FROM normalized.station_versions) AS versions,
                      (SELECT count(*) FROM normalized.collections) AS collections,
                      (SELECT count(*) FROM normalized.observations) AS observations,
                      (SELECT count(*) FROM normalized.unobserved_stations) AS unobserved"""
        ).fetchone()
        # gbfs and station information bodies shared; two distinct status bodies.
        assert counts == {
            "bodies": 4,
            "versions": 4,
            "collections": 2,
            "observations": 4,
            "unobserved": 1,
        }
        assert (
            conn.execute("SELECT to_regclass('normalized.station_snapshots') AS t").fetchone()["t"]
            is None
        )
    assert init_db(old_database) == []  # Applied once; a rerun does nothing.
