"""Offline parsing checks using small, documented extracts of real GBFS responses."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from transit.bikeshare.parsing import (
    FeedValidationError,
    discover,
    envelope,
    parse_information,
    parse_status,
    timestamp,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def information():
    return json.loads((FIXTURES / "station_information.json").read_text(encoding="utf-8"))


@pytest.fixture
def status():
    return json.loads((FIXTURES / "station_status.json").read_text(encoding="utf-8"))


@pytest.fixture
def discovery():
    return json.loads((FIXTURES / "discovery.json").read_text(encoding="utf-8"))


def test_representative_real_responses(information, status, discovery):
    stations = parse_information(information)
    observations = parse_status(status)
    assert [s["station_id"] for s in stations] == ["7000", "7001"]
    assert stations[0] == {
        "station_id": "7000",
        "name": "Fort York  Blvd / Capreol Ct",
        "lat": 43.63970679931218,
        "lon": -79.39614309573555,
        "capacity": 47,
    }
    assert observations[0]["num_bikes_available"] == 42
    assert observations[0]["num_docks_available"] == 3
    assert observations[0]["num_bikes_disabled"] == 2
    assert observations[0]["station_reported_at"] == datetime(
        2026, 10, 9, 9, 38, 28, 628000, tzinfo=UTC
    )
    assert observations[1]["num_docks_available"] == 0
    assert discover(discovery)["station_status"] == (
        "https://toronto.publicbikesystem.net/customer/gbfs/v3.0/station_status"
    )
    # This real example proves capacity cannot be bikes available + docks available.
    assert (
        observations[0]["num_bikes_available"] + observations[0]["num_docks_available"]
        != stations[0]["capacity"]
    )


@pytest.mark.parametrize("payload", [None, [], "not an object", 42])
def test_malformed_root_is_rejected(payload):
    with pytest.raises(FeedValidationError, match="root"):
        envelope(payload)


@pytest.mark.parametrize("field", ["version", "last_updated", "ttl", "data"])
def test_missing_envelope_field_is_rejected(status, field):
    del status[field]
    with pytest.raises(FeedValidationError):
        parse_status(status)


@pytest.mark.parametrize("value", ["2.3", "4.0", 3, None])
def test_unsupported_version_is_explicit(status, value):
    status["version"] = value
    with pytest.raises(FeedValidationError, match="3.0"):
        parse_status(status)


@pytest.mark.parametrize("value", [None, {}, [], "stations", [None]])
def test_malformed_or_empty_stations_cannot_be_success(status, value):
    status["data"]["stations"] = value
    with pytest.raises(FeedValidationError):
        parse_status(status)


@pytest.mark.parametrize(
    "field",
    [
        "station_id",
        "num_vehicles_available",
        "num_docks_available",
        "last_reported",
        "is_installed",
        "is_renting",
        "is_returning",
    ],
)
def test_missing_required_status_field_fails_whole_feed(status, field):
    # Put the malformed record second, so an implementation cannot return partial data.
    del status["data"]["stations"][1][field]
    with pytest.raises(FeedValidationError, match=field):
        parse_status(status)


@pytest.mark.parametrize("value", [True, False, "3", 3.2, -1, None])
def test_counts_reject_booleans_strings_and_invalid_numbers(status, value):
    status["data"]["stations"][0]["num_vehicles_available"] = value
    with pytest.raises(FeedValidationError, match="num_vehicles_available"):
        parse_status(status)


@pytest.mark.parametrize("value", [1, 0, "true", "false", None])
def test_service_flags_require_actual_booleans(status, value):
    status["data"]["stations"][0]["is_renting"] = value
    with pytest.raises(FeedValidationError, match="is_renting"):
        parse_status(status)


def test_inactive_station_is_preserved_without_inventing_zero_counts(status):
    status["data"]["stations"][0].update(is_installed=False, is_renting=False, is_returning=False)
    row = parse_status(status)[0]
    assert row["is_installed"] is False
    assert row["is_renting"] is False
    assert row["is_returning"] is False
    assert row["num_bikes_available"] == 42


def test_missing_optional_values_remain_unknown(information, status):
    del information["data"]["stations"][0]["capacity"]
    del status["data"]["stations"][0]["num_vehicles_disabled"]
    del status["data"]["stations"][0]["num_docks_disabled"]
    assert parse_information(information)[0]["capacity"] is None
    row = parse_status(status)[0]
    assert row["num_bikes_disabled"] is None
    assert row["num_docks_disabled"] is None


def test_invalid_optional_count_is_not_silently_dropped(status):
    status["data"]["stations"][0]["num_vehicles_disabled"] = "unknown"
    with pytest.raises(FeedValidationError, match="num_vehicles_disabled"):
        parse_status(status)


def test_unknown_extensions_are_ignored_but_input_is_not_modified(status):
    status["data"]["stations"][0]["vendor_extension"] = {"anything": [1, 2, 3]}
    original = deepcopy(status)
    assert "vendor_extension" not in parse_status(status)[0]
    assert status == original


@pytest.mark.parametrize(
    "fixture_name,parser",
    [
        ("information", parse_information),
        ("status", parse_status),
    ],
)
def test_duplicate_station_ids_are_rejected(request, fixture_name, parser):
    payload = request.getfixturevalue(fixture_name)
    payload["data"]["stations"].append(deepcopy(payload["data"]["stations"][0]))
    with pytest.raises(FeedValidationError, match="Duplicate station_id"):
        parser(payload)


@pytest.mark.parametrize("value", [None, "", "  ", 7000])
def test_invalid_station_identity_is_rejected(status, value):
    status["data"]["stations"][0]["station_id"] = value
    with pytest.raises(FeedValidationError, match="station_id"):
        parse_status(status)


@pytest.mark.parametrize("field", ["name", "lat", "lon"])
def test_missing_required_metadata_is_rejected(information, field):
    del information["data"]["stations"][0][field]
    with pytest.raises(FeedValidationError, match=field):
        parse_information(information)


@pytest.mark.parametrize(
    "field,value",
    [
        ("lat", 91),
        ("lat", float("nan")),
        ("lon", float("inf")),
        ("lon", -181),
        ("lat", True),
        ("lon", "-79.3"),
    ],
)
def test_invalid_coordinates_are_rejected(information, field, value):
    information["data"]["stations"][0][field] = value
    with pytest.raises(FeedValidationError, match=field):
        parse_information(information)


@pytest.mark.parametrize(
    "value",
    [
        "untranslated name",
        [],
        [{}],
        [{"text": "", "language": "en"}],
        [{"text": "Valid name", "language": 3}],
    ],
)
def test_malformed_translated_name_is_rejected(information, value):
    information["data"]["stations"][0]["name"] = value
    with pytest.raises(FeedValidationError, match="name"):
        parse_information(information)


def test_english_name_preferred_with_documented_first_translation_fallback(information):
    row = information["data"]["stations"][0]
    row["name"] = [{"text": "Français", "language": "fr"}, {"text": "English", "language": "en"}]
    assert parse_information(information)[0]["name"] == "English"
    row["name"].pop()
    assert parse_information(information)[0]["name"] == "Français"


def test_timestamp_offsets_and_dst_overlap_preserve_distinct_instants():
    first = timestamp("2026-11-01T01:30:00-04:00")
    second = timestamp("2026-11-01T01:30:00-05:00")
    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
    assert (second - first).total_seconds() == 3600


@pytest.mark.parametrize(
    "value",
    [
        None,
        1791532800,
        "not a timestamp",
        "2026-10-09",
        "2026-10-09T12:00:00",
        "2026-02-30T12:00:00Z",
    ],
)
def test_invalid_or_naive_timestamps_are_rejected(value):
    with pytest.raises(FeedValidationError):
        timestamp(value)


def test_feed_and_station_timestamps_remain_distinct(status):
    published = timestamp(status["last_updated"])
    reported = parse_status(status)[0]["station_reported_at"]
    assert published > reported
    assert (published - reported).total_seconds() == pytest.approx(78.372)


def test_old_station_report_is_preserved_for_downstream_freshness_checks(status):
    # Parsing must not replace old source timestamps with collection time.
    status["data"]["stations"][0]["last_reported"] = "2020-01-01T00:00:00Z"
    assert parse_status(status)[0]["station_reported_at"] == datetime(2020, 1, 1, tzinfo=UTC)


def test_discovery_requires_both_station_feeds(discovery):
    discovery["data"]["feeds"] = [
        f for f in discovery["data"]["feeds"] if f["name"] != "station_status"
    ]
    with pytest.raises(FeedValidationError, match="missing"):
        discover(discovery)


def test_discovery_rejects_duplicate_feed_names(discovery):
    discovery["data"]["feeds"].append(deepcopy(discovery["data"]["feeds"][0]))
    with pytest.raises(FeedValidationError, match="Duplicate feed"):
        discover(discovery)


@pytest.mark.parametrize("value", ["http://example.test/feed", "file:///secret", "/relative", 4])
def test_discovery_rejects_invalid_feed_urls(discovery, value):
    discovery["data"]["feeds"][0]["url"] = value
    with pytest.raises(FeedValidationError):
        discover(discovery)
