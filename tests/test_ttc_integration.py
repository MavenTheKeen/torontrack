"""TTC storage, replay, versioning and retention in a disposable *_test database."""

from datetime import UTC, datetime, timedelta

import pytest

from tests.ttc_helpers import T0, alert_feed, static_zip, trip_feed, vehicle_feed
from transit.db import connect
from transit.http import FetchError
from transit.ttc import collector, static_gtfs, store
from transit.ttc.sources import REALTIME_FEEDS

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(settings):
    with connect(settings.database_url) as connection:
        assert static_gtfs.load(connection, static_gtfs.parse(static_zip()), "test://gtfs", T0)
        yield connection


def one(conn, sql, params=()):
    return list(conn.execute(sql, params).fetchone().values())[0]


def ingest(conn, feed, payload, fetched_at):
    snapshot_id, is_new = store.store_snapshot(conn, feed, f"test://{feed}", payload, fetched_at)
    return store.normalize_snapshot(conn, snapshot_id), is_new


def test_static_version_load_is_idempotent(conn):
    feed = static_gtfs.parse(static_zip())
    assert static_gtfs.load(conn, feed, "test://gtfs", T0) is False
    assert one(conn, "SELECT count(*) FROM normalized.ttc_gtfs_versions") == 1
    assert one(conn, "SELECT count(*) FROM normalized.ttc_stop_times") == len(feed.stop_times)


def test_repeated_feed_timestamp_is_one_snapshot_and_replay_is_stable(conn):
    trips = [{"trip_id": "1", "route_id": "1", "stops": [("13806", 3, 60), ("13807", 4, 60)]}]
    result, is_new = ingest(conn, "trips_subway", trip_feed(T0, trips), T0)
    assert is_new and result["accepted"] == 2 and result["flagged"] == 1
    _, is_new = ingest(conn, "trips_subway", trip_feed(T0, trips), T0 + timedelta(seconds=30))
    assert not is_new
    assert one(conn, "SELECT count(*) FROM raw.ttc_realtime_snapshots") == 1
    assert one(conn, "SELECT count(*) FROM ops.ttc_record_issues") == 1  # Not duplicated.
    assert one(conn, "SELECT count(*) FROM normalized.ttc_train_stop_events") == 2


def test_latest_prediction_wins_even_when_replayed_out_of_order(conn):
    first = trip_feed(T0, [{"trip_id": "1", "route_id": "1", "stops": [("13807", 4, 300)]}])
    later_time = T0 + timedelta(seconds=30)
    later = trip_feed(later_time, [{"trip_id": "1", "route_id": "1", "stops": [("13807", 4, 330)]}])
    (first_result, _), (later_result, _) = (
        ingest(conn, "trips_subway", first, T0),
        ingest(conn, "trips_subway", later, later_time),
    )
    expected = later_time + timedelta(seconds=330)
    store.normalize_snapshot(conn, first_result["snapshot_id"])  # Replay the older snapshot.
    event = conn.execute("SELECT * FROM normalized.ttc_train_stop_events").fetchone()
    assert event["predicted_arrival"] == expected
    assert (event["first_seen_at"], event["last_seen_at"]) == (T0, later_time)
    current = conn.execute("SELECT * FROM normalized.ttc_current_predictions").fetchall()
    assert [r["snapshot_id"] for r in current] == [later_result["snapshot_id"]]


def test_current_predictions_follow_the_newest_snapshot(conn):
    ingest(
        conn,
        "trips_subway",
        trip_feed(
            T0,
            [
                {"trip_id": "1", "route_id": "1", "stops": [("13806", 3, 60)]},
            ],
        ),
        T0,
    )
    later = T0 + timedelta(seconds=30)
    ingest(conn, "trips_subway", trip_feed(later, []), later)  # Trip left the feed.
    assert one(conn, "SELECT count(*) FROM normalized.ttc_current_predictions") == 0
    assert one(conn, "SELECT count(*) FROM normalized.ttc_train_stop_events") == 1


def test_platform_dropped_while_trip_continues_is_marked_passed(conn):
    def snap(at, stops):
        return trip_feed(at, [{"trip_id": "1", "route_id": "1", "stops": stops}])

    t1, t2, t3 = (T0 + timedelta(seconds=s) for s in (0, 30, 60))
    ingest(conn, "trips_subway", snap(t1, [("13806", 3, 20), ("13807", 4, 110)]), t1)
    ingest(conn, "trips_subway", snap(t2, [("13807", 4, 80)]), t2)
    rows = conn.execute("SELECT stop_id, passed_at FROM normalized.ttc_train_stop_events")
    assert {r["stop_id"]: r["passed_at"] for r in rows} == {"13806": t2, "13807": None}
    # The source lists the platform again: the earlier "passed" mark is withdrawn.
    ingest(conn, "trips_subway", snap(t3, [("13806", 3, 5), ("13807", 4, 50)]), t3)
    assert (
        one(
            conn,
            "SELECT count(*) FROM normalized.ttc_train_stop_events WHERE passed_at IS NOT NULL",
        )
        == 0
    )


def test_early_drop_is_not_an_arrival_but_a_departed_train_is(conn):
    t1, t2 = T0, T0 + timedelta(seconds=30)
    first = trip_feed(
        t1,
        [
            # Due at College in 10 minutes, then the platform is withdrawn (short turn).
            {
                "trip_id": "1",
                "vehicle": "838",
                "route_id": "1",
                "stops": [("13806", 3, 20), ("13807", 4, 600)],
            },
            # Due at Wellesley now, then the train leaves the feed (e.g. at a terminal).
            {"trip_id": "2", "vehicle": "131", "route_id": "1", "stops": [("13806", 3, 30)]},
        ],
    )
    later = trip_feed(
        t2, [{"trip_id": "1", "vehicle": "838", "route_id": "1", "stops": [("13808", 1, 60)]}]
    )
    ingest(conn, "trips_subway", first, t1)
    ingest(conn, "trips_subway", later, t2)
    rows = conn.execute(
        "SELECT train_id, stop_id, passed_at FROM normalized.ttc_train_stop_events"
    ).fetchall()
    passed = {(r["train_id"], r["stop_id"]): r["passed_at"] for r in rows}
    assert passed[("838", "13806")] == t2
    assert passed[("838", "13807")] is None  # Withdrawn 10 minutes early: not an arrival.
    assert passed[("131", "13806")] == t2  # Train gone from the feed, but it was due.


def test_sudden_empty_snapshot_is_skipped_until_the_feed_stays_empty(conn):
    def snap(at, count):
        trains = [
            {"trip_id": str(i), "route_id": "1", "stops": [("13806", 3, 30)]} for i in range(count)
        ]
        return trip_feed(at, trains)

    def passed():
        return one(
            conn,
            "SELECT count(*) FROM normalized.ttc_train_stop_events WHERE passed_at IS NOT NULL",
        )

    t1, t2, t3 = (T0 + timedelta(seconds=s) for s in (0, 30, 60))
    ingest(conn, "trips_subway", snap(t1, 12), t1)
    result, _ = ingest(conn, "trips_subway", snap(t2, 0), t2)  # TTC blip: no trains listed.
    assert result["accepted"] == 0 and result["rejected"] == 1
    assert one(conn, "SELECT count(*) FROM normalized.ttc_current_predictions") == 12
    assert passed() == 0  # Trains due now were not marked as arrived.
    assert one(conn, "SELECT issue FROM ops.ttc_record_issues") == "feed_dropout"
    ingest(conn, "trips_subway", snap(t3, 5), t3)  # Partial snapshot: also skipped.
    assert one(conn, "SELECT count(*) FROM normalized.ttc_current_predictions") == 12

    # Still empty after the window: the trains really are gone.
    gone = t1 + store.DROPOUT_WINDOW + timedelta(seconds=30)
    ingest(conn, "trips_subway", snap(gone, 0), gone)
    assert one(conn, "SELECT count(*) FROM normalized.ttc_current_predictions") == 0


def test_churning_trip_ids_for_one_train_are_one_visit(conn):
    # Observed live: the same train (vehicle 118) gets a new trip_id on most polls.
    for n, offset in enumerate((0, 30, 60)):
        at = T0 + timedelta(seconds=offset)
        trip = {"trip_id": f"13492868{n}", "vehicle": "118", "route_id": "1"}
        ingest(conn, "trips_subway", trip_feed(at, [{**trip, "stops": [("13807", 4, 90)]}]), at)
    event = conn.execute("SELECT * FROM normalized.ttc_train_stop_events").fetchall()
    assert len(event) == 1
    assert event[0]["train_id"] == "118"
    assert event[0]["last_trip_id"] == "134928682"
    # A visit to the same platform on the train's next round trip is a new visit.
    later = T0 + timedelta(minutes=45)
    trip = {"trip_id": "134929000", "vehicle": "118", "route_id": "1"}
    ingest(conn, "trips_subway", trip_feed(later, [{**trip, "stops": [("13807", 4, 60)]}]), later)
    assert one(conn, "SELECT count(*) FROM normalized.ttc_train_stop_events") == 2


def test_alert_versions_record_first_and_last_seen(conn):
    base = {"id": "500", "effect": "REDUCED_SERVICE", "routes": ["2"]}
    times = [T0, T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)]
    texts = ["Line 2: no subway service between Kipling and Jane", "same", "Line 2: delays"]
    texts[1] = texts[0]
    for at, text in zip(times, texts, strict=True):
        ingest(conn, "alerts_subway", alert_feed(at, [{**base, "text": text}]), at)
    versions = conn.execute(
        """SELECT * FROM normalized.ttc_alert_versions ORDER BY first_seen_at"""
    ).fetchall()
    assert [(v["first_seen_at"], v["last_seen_at"]) for v in versions] == [
        (times[0], times[1]),
        (times[2], times[2]),
    ]
    # The more severe of effect (REDUCED_SERVICE) and text wins; both disagree with effect.
    assert [v["derived_status"] for v in versions] == ["no_service", "reduced_service"]
    assert [v["status_mismatch"] for v in versions] == [True, True]
    alert = conn.execute("SELECT * FROM normalized.ttc_alerts").fetchone()
    assert alert["current_version_hash"] == versions[1]["version_hash"]
    assert (
        one(
            conn,
            "SELECT count(*) FROM ops.ttc_record_issues WHERE issue='alert_effect_text_mismatch'",
        )
        == 3  # One per snapshot.
    )


def test_accessibility_alert_lines_come_from_static_platforms(conn):
    payload = alert_feed(
        T0,
        [
            {
                "id": "77",
                "effect": "ACCESSIBILITY_ISSUE",
                "stops": ["13864", "13756"],
                "text": "Bloor-Yonge: Elevator out of service",
            }
        ],
    )
    ingest(conn, "alerts_accessibility", payload, T0)
    version = conn.execute("SELECT * FROM normalized.ttc_alert_versions").fetchone()
    assert version["route_ids"] == ["1", "2"]
    assert version["stop_ids"] == ["13756", "13864"]


def test_retention_keeps_current_snapshot_and_recent_history(conn):
    old = datetime.now(UTC) - timedelta(days=10)
    for offset in (0, 30):
        at = old + timedelta(seconds=offset)
        ingest(
            conn,
            "trips_subway",
            trip_feed(
                at,
                [
                    {"trip_id": "1", "route_id": "1", "stops": [("13806", 3, 60)]},
                ],
            ),
            at,
        )
    deleted = store.apply_retention(conn, raw_days=3, event_days=5)
    assert deleted["raw_snapshots"] == 1  # The newest (current) snapshot is kept.
    assert deleted["train_stop_events"] == 1
    assert one(conn, "SELECT count(*) FROM raw.ttc_realtime_snapshots") == 1


def test_event_indexes_are_rebuilt_in_place(conn):
    ingest(
        conn,
        "trips_subway",
        trip_feed(T0, [{"trip_id": "1", "route_id": "1", "stops": [("13806", 3, 60)]}]),
        T0,
    )
    sizes = store.compact_event_indexes(conn)
    assert sizes["index_bytes_after"] > 0
    assert one(conn, "SELECT count(*) FROM normalized.ttc_train_stop_events") == 1


class FakeClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def fetch_bytes(self, url, *, host, max_bytes):
        self.calls.append(url)
        payload = self.payloads[url]
        if isinstance(payload, Exception):
            raise payload
        return payload


def test_poll_records_runs_unchanged_and_backoff(conn, settings):
    url = REALTIME_FEEDS["trips_subway"]
    retry_at = datetime.now(UTC) + timedelta(minutes=5)
    client = FakeClient(
        {
            url: trip_feed(T0, []),
            REALTIME_FEEDS["alerts_subway"]: b"\xff\xff",
            REALTIME_FEEDS["alerts_accessibility"]: FetchError(
                "HTTP 429: retries exhausted", retry_not_before=retry_at
            ),
            REALTIME_FEEDS["vehicles"]: vehicle_feed(T0, []),
        }
    )
    first = {r["feed"]: r for r in collector.poll_once(settings.database_url, client)}
    assert first["trips_subway"]["status"] == "stored"
    assert first["alerts_subway"]["status"] == "failed"
    assert "not a GTFS-Realtime" in first["alerts_subway"]["error"]
    assert first["alerts_accessibility"]["status"] == "failed"
    assert first["vehicles"]["status"] == "stored"
    second = {r["feed"]: r for r in collector.poll_once(settings.database_url, client)}
    assert second["trips_subway"]["status"] == "unchanged"
    assert "Retry-After is still active" in second["alerts_accessibility"]["error"]
    assert client.calls.count(REALTIME_FEEDS["alerts_accessibility"]) == 1
    assert one(conn, "SELECT count(*) FROM ops.ttc_poll_runs") == 2 * len(REALTIME_FEEDS)


def test_predictions_and_positions_are_polled_more_often_than_alerts():
    # At the start every feed is due; then only the fast ones until the slow interval passes.
    assert collector.due_feeds(0, {}, 30, 10) == list(REALTIME_FEEDS)
    last = dict.fromkeys(REALTIME_FEEDS, 0.0)
    assert collector.due_feeds(10, last, 30, 10) == ["trips_subway", "vehicles"]
    assert collector.due_feeds(5, last, 30, 10) == []
    assert collector.due_feeds(30, last, 30, 10) == list(REALTIME_FEEDS)
