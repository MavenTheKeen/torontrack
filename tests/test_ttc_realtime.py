"""Realtime decoding and the validation layer, offline (constructed feeds + one real capture)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from google.transit import gtfs_realtime_pb2

from tests.ttc_helpers import T0, static_zip, trip_feed
from transit.bikeshare.parsing import FeedValidationError
from transit.ttc import realtime
from transit.ttc.static_gtfs import parse

FIXTURES = Path(__file__).parent / "fixtures" / "ttc"


@pytest.fixture(scope="module")
def stops():
    feed = parse(static_zip())
    return {r["stop_id"]: r for r in feed.line_stops}


def issues_by_name(issues):
    return {(i.issue, i.action) for i in issues}


def test_decode_rejects_garbage_and_incomplete_headers():
    with pytest.raises(FeedValidationError, match="not a GTFS-Realtime"):
        realtime.decode(b"\xff\xff\xff")
    message = gtfs_realtime_pb2.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    with pytest.raises(FeedValidationError, match="no timestamp"):
        realtime.decode(message.SerializeToString())
    message.header.timestamp = 1
    message.header.incrementality = gtfs_realtime_pb2.FeedHeader.DIFFERENTIAL
    with pytest.raises(FeedValidationError, match="FULL_DATASET"):
        realtime.decode(message.SerializeToString())


def test_clean_trip_is_accepted_in_line_order(stops):
    payload = trip_feed(
        T0,
        [{"trip_id": "9001", "route_id": "1", "stops": [("13806", 3, 60), ("13807", 4, 150)]}],
    )
    rows, issues = realtime.parse_trip_updates(realtime.decode(payload), stops)
    assert issues == []
    assert [r["stop_id"] for r in rows] == ["13806", "13807"]
    assert rows[0]["direction_id"] == 0
    assert rows[0]["direction_label"] == "South"
    assert rows[1]["predicted_arrival"] == T0 + timedelta(seconds=150)


def test_observed_ttc_quirks_are_flagged_not_dropped(stops):
    payload = trip_feed(
        T0,
        [
            # Identical arrival times at consecutive platforms (seen in the live feed).
            {"trip_id": "1", "route_id": "1", "stops": [("13806", 3, 60), ("13807", 4, 60)]},
            # Stop sequence out of order relative to the line.
            {"trip_id": "2", "route_id": "1", "stops": [("13807", 4, 120), ("13806", 3, 60)]},
        ],
    )
    rows, issues = realtime.parse_trip_updates(realtime.decode(payload), stops)
    assert len(rows) == 4
    assert issues_by_name(issues) == {
        ("identical_consecutive_arrival", "flagged"),
        ("out_of_order_stop_sequence", "flagged"),
        ("feed_order_differs_from_line_order", "flagged"),
    }
    trip_2 = [r["stop_id"] for r in rows if r["trip_id"] == "2"]
    assert trip_2 == ["13806", "13807"]  # Re-sorted into line order.


def test_invalid_records_are_rejected_with_reasons(stops):
    payload = trip_feed(
        T0,
        [
            {
                "trip_id": "3",
                "route_id": "1",
                "stops": [
                    ("99999", 1, 60),  # unknown platform
                    ("13756", 2, 60),  # Line 2 platform on a Line 1 trip
                    ("13806", 3, 4 * 3600),  # four hours ahead
                    ("13807", 4, 120),
                    ("13807", 4, 125),  # duplicate stop
                ],
            },
            {"trip_id": "3", "route_id": "1", "stops": [("13807", 4, 120)]},  # duplicate trip
        ],
    )
    rows, issues = realtime.parse_trip_updates(realtime.decode(payload), stops)
    assert [r["stop_id"] for r in rows] == ["13807"]
    assert issues_by_name(issues) == {
        ("unknown_stop", "rejected"),
        ("stop_route_mismatch", "rejected"),
        ("implausible_time", "rejected"),
        ("duplicate_stop", "rejected"),
        ("missing_or_duplicate_trip_id", "rejected"),
    }


def test_train_identity_is_the_vehicle_label(stops):
    payload = trip_feed(
        T0,
        [
            {"trip_id": "5", "vehicle": "118", "route_id": "1", "stops": [("13806", 3, 60)]},
            {"trip_id": "6", "vehicle": "118", "route_id": "1", "stops": [("13807", 4, 60)]},
            {"trip_id": "7", "vehicle": "", "route_id": "1", "stops": [("13803", 1, 60)]},
        ],
    )
    rows, issues = realtime.parse_trip_updates(realtime.decode(payload), stops)
    assert [(r["train_id"], r["trip_id"]) for r in rows] == [("118", "5"), ("trip-7", "7")]
    assert issues_by_name(issues) == {
        ("duplicate_vehicle", "rejected"),
        ("missing_vehicle_label", "flagged"),
    }


def test_platforms_from_both_directions_keep_majority(stops):
    payload = trip_feed(
        T0,
        [
            {
                "trip_id": "4",
                "route_id": "1",
                "stops": [("13806", 3, 60), ("13807", 4, 120), ("13863", 3, 180)],
            }
        ],
    )
    rows, issues = realtime.parse_trip_updates(realtime.decode(payload), stops)
    assert {r["direction_id"] for r in rows} == {0}
    assert issues_by_name(issues) == {("direction_conflict", "rejected")}


def test_service_date_rolls_over_at_4am_toronto():
    assert str(realtime.service_date(datetime(2026, 10, 10, 6, 30, tzinfo=UTC))) == "2026-10-09"
    assert str(realtime.service_date(datetime(2026, 10, 10, 8, 30, tzinfo=UTC))) == "2026-10-10"


def test_stale_feed_timestamp_is_flagged():
    snapshot = realtime.decode(trip_feed(T0, []))
    assert realtime.feed_issues(snapshot, T0 + timedelta(seconds=40)) == []
    stale = realtime.feed_issues(snapshot, T0 + timedelta(minutes=10))
    assert stale[0].issue == "stale_feed_timestamp"
    assert stale[0].detail == {"age_seconds": 600}


@pytest.mark.parametrize(
    ("effect", "text", "derived", "mismatch"),
    [
        (
            "REDUCED_SERVICE",
            "There will be no subway service between Kipling and Jane stations",
            "no_service",
            True,
        ),
        ("NO_SERVICE", "Trains are not stopping at Osgoode station", "no_service", False),
        ("UNKNOWN_EFFECT", "Delays of up to 10 minutes", "delays", False),
        ("ACCESSIBILITY_ISSUE", "Elevator 7B1L out of service", "accessibility", False),
        ("MODIFIED_SERVICE", "Trains will run every 6 minutes", "modified_service", False),
        ("UNKNOWN_EFFECT", "Customer information", "other", False),
    ],
)
def test_alert_status_uses_effect_and_text(effect, text, derived, mismatch):
    status = realtime.classify_alert(effect, text)
    assert status["derived_status"] == derived
    assert status["status_mismatch"] is mismatch


def test_real_captured_alerts_flag_the_effect_text_contradiction():
    snapshot = realtime.decode((FIXTURES / "alerts_subway.pb").read_bytes())
    alerts, issues = realtime.parse_alerts(snapshot)
    by_id = {a["alert_id"]: a for a in alerts}
    assert set(by_id) == {"77794", "77528", "77523"}
    assert by_id["77794"]["cause"] == "POLICE_ACTIVITY"
    assert by_id["77523"]["effect"] == "REDUCED_SERVICE"
    assert by_id["77523"]["derived_status"] == "no_service"
    assert [(i.issue, i.entity_id) for i in issues] == [("alert_effect_text_mismatch", "77523")]
    # Planned closure: the active period starts after the snapshot.
    start = by_id["77528"]["active_periods"][0]["start"]
    assert datetime.fromtimestamp(start, UTC) > snapshot.feed_timestamp
    # 77523 is the advance notice for that closure; its active period is the week the
    # notice is shown, so it must not count as a current disruption.
    assert by_id["77523"]["advance_notice"] is True
    assert by_id["77528"]["advance_notice"] is False
    assert by_id["77794"]["advance_notice"] is False
