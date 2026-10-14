"""Bus and streetcar stop passages: measurement stops, crossing detection and recording."""

from datetime import timedelta

import pytest

from tests.ttc_helpers import T0, static_zip, vehicle_feed
from transit.db import connect
from transit.ttc import matching, passages, static_gtfs, store, surface


def test_measurement_stops_fall_every_ten_scheduled_minutes_but_not_at_terminals():
    offsets = [0, 120, 400, 610, 900, 1150, 1260, 1800]
    # 610 enters the 600 block and 1260 the 1200 block; the last stop is a terminal.
    assert matching.measured_stops(offsets) == [3, 6]
    # Too short to cross a block: the middle stop.
    assert matching.measured_stops([0, 100, 200, 300, 400]) == [2]
    assert matching.measured_stops([0, 60]) == [1]


PATTERN = {"pattern_id": 7, "measured": [2, 4], "stops": ["a", "b", "c", "d", "e"]}


def placed(position, seconds, pattern_id=7):
    return {
        "pattern_id": pattern_id,
        "position": position,
        "reported_at": T0 + timedelta(seconds=seconds),
    }


def test_a_crossing_is_timed_between_the_two_reports():
    [(k, at, gap)] = passages.crossings(placed(1.5, 0), placed(2.5, 40), PATTERN)
    assert (k, at, gap) == (2, T0 + timedelta(seconds=20), 40)
    both = passages.crossings(placed(1.0, 0), placed(4.0, 60), PATTERN)
    assert [c[0] for c in both] == [2, 4]


def test_untrustworthy_steps_record_nothing():
    assert passages.crossings(None, placed(2.5, 40), PATTERN) == []
    assert passages.crossings(placed(1.5, 0, pattern_id=8), placed(2.5, 40), PATTERN) == []
    assert passages.crossings(placed(1.5, 0), placed(2.5, 600), PATTERN) == []  # Long silence.
    assert passages.crossings(placed(2.5, 0), placed(1.5, 40), PATTERN) == []  # Backwards.
    far = {**PATTERN, "measured": [20]}
    assert passages.crossings(placed(0.5, 0), placed(20.5, 40), far) == []  # Implausible jump.


def snapshot(conn, seconds, vehicles):
    at = T0 + timedelta(seconds=seconds)
    snapshot_id, _ = store.store_snapshot(
        conn, "vehicles", "test://", vehicle_feed(at, vehicles), at
    )
    store.normalize_snapshot(conn, snapshot_id)
    return at


@pytest.mark.integration
def test_record_times_a_passage_and_resets_on_a_new_trip(settings):
    with connect(settings.database_url) as conn:
        feed = static_gtfs.parse(static_zip())
        static_gtfs.load(conn, feed, "test://gtfs", T0)
        surface.load(conn, surface.parse(static_zip()), feed.feed_version)
        matching._index.clear()
        bus = {"id": "8001", "route_id": "10", "bearing": 90, "lat": 43.7}
        # Eastbound 10 Van Horne, whose measured stop is the middle one (Midland Ave):
        # halfway between the first two stops, then three quarters of the way to Brimley,
        # then at Brimley.
        positions = [-79.265, -79.2525, -79.25]
        results = []
        for i, lon in enumerate(positions):
            at = snapshot(conn, 60 * i, [{**bus, "lon": lon}])
            results.append(passages.record(conn, feed.feed_version, at))
        assert [r["passages"] for r in results] == [0, 1, 0]
        [row] = conn.execute("SELECT * FROM normalized.ttc_surface_passages").fetchall()
        assert (row["route_id"], row["direction_id"], row["stop_id"]) == ("10", 0, "663")
        # From position 0.5 to 1.75 between reports 60 s apart (each 20 s older than its
        # snapshot), stop 1 is 40% of the way: 24 s after the first report.
        expected = T0 + timedelta(seconds=-20 + 24)
        assert abs((row["passed_at"] - expected).total_seconds()) < 0.5
        assert row["report_gap_seconds"] == 60
        # Turning back west is a new trip on another pattern: no passage is invented.
        at = snapshot(conn, 180, [{**bus, "bearing": 270, "lat": 43.7003, "lon": -79.255}])
        assert passages.record(conn, feed.feed_version, at)["passages"] == 0
        # The same report seen again is skipped.
        assert passages.record(conn, feed.feed_version, at)["tracked"] == 0
