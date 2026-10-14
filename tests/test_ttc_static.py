"""Static GTFS parsing: subway subset, station naming, line order, and rejection rules."""

import hashlib
import io
import zipfile
from datetime import date

import pytest

from tests.ttc_helpers import LINE_1_NORTH, LINE_1_SOUTH, LINE_2_EAST, static_zip
from transit.bikeshare.parsing import FeedValidationError
from transit.ttc.static_gtfs import parse, seconds, station_key, station_parts


@pytest.mark.parametrize(
    ("stop_name", "expected"),
    [
        ("College Station - Southbound Platform", ("College", "Southbound")),
        (
            "Union Station - Northbound Platform Towards Vaughan Metropolitan Centre",
            ("Union", "Northbound towards Vaughan Metropolitan Centre"),
        ),
        ("Kipling Station - Subway Platform", ("Kipling", "Subway")),
        ("York University - Northbound Platform", ("York University", "Northbound")),
        ("Bloor Station - Southbound Platform", ("Bloor-Yonge", "Southbound")),
        ("Yonge Station - Eastbound Platform", ("Bloor-Yonge", "Eastbound")),
    ],
)
def test_station_parts_from_real_ttc_platform_names(stop_name, expected):
    assert station_parts(stop_name) == expected


def test_station_key_is_url_safe():
    assert station_key("Queen's Park") == "queens-park"
    assert station_key("Vaughan Metropolitan Centre") == "vaughan-metropolitan-centre"


def test_gtfs_times_past_midnight():
    assert seconds("25:01:30") == 90090


def test_parse_keeps_only_subway_in_line_order():
    payload = static_zip()
    feed = parse(payload)
    assert feed.feed_version == hashlib.sha256(payload).hexdigest()
    assert {r["route_id"] for r in feed.routes} == {"1", "2"}
    assert {s["stop_id"] for s in feed.stops} == set(LINE_1_SOUTH + LINE_1_NORTH + LINE_2_EAST)
    assert all(t["route_id"] != "10" for t in feed.trips)
    south = [
        r["stop_id"] for r in feed.line_stops if (r["route_id"], r["direction_id"]) == ("1", 0)
    ]
    assert south == LINE_1_SOUTH
    towards = {(r["route_id"], r["direction_id"]): r["towards"] for r in feed.line_stops}
    assert towards == {
        ("1", 0): "Vaughan Metropolitan Centre",
        ("1", 1): "Finch",
        ("2", 0): "Kennedy",
    }
    keys = {s["stop_id"]: s["station_key"] for s in feed.stops}
    assert keys["13864"] == keys["13756"] == "bloor-yonge"
    assert (feed.service_start, feed.service_end) == (date(2026, 9, 30), date(2026, 10, 31))


def test_platform_outside_every_canonical_pattern_is_rejected():
    # A one-off trip using a platform that no line's most common pattern contains.
    extra = [("1-short", "07:00:00", "07:00:00", "13811", 1)]
    payload = static_zip(extra)
    archive = zipfile.ZipFile(io.BytesIO(payload))
    files = {n: archive.read(n).decode() for n in archive.namelist()}
    files["trips.txt"] += "1,1,1-short,Line 1 short turn,0\n"
    files["stops.txt"] += "13811,Queen Station - Southbound Platform,43.65,-79.38,1\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name, text in files.items():
            out.writestr(name, text)
    with pytest.raises(FeedValidationError, match="outside every canonical pattern"):
        parse(buffer.getvalue())


def test_trip_using_another_lines_platform_is_rejected():
    extra = [("1-cross", "07:00:00", "07:00:00", "13756", 1)]  # Line 2 platform
    archive = zipfile.ZipFile(io.BytesIO(static_zip(extra)))
    files = {n: archive.read(n).decode() for n in archive.namelist()}
    files["trips.txt"] += "1,1,1-cross,Line 1 cross,0\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as out:
        for name, text in files.items():
            out.writestr(name, text)
    with pytest.raises(FeedValidationError, match="platform of another line"):
        parse(buffer.getvalue())


def test_missing_required_file_and_invalid_zip_are_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("routes.txt", "route_id,route_type\n")
    with pytest.raises(FeedValidationError, match="missing trips.txt"):
        parse(buffer.getvalue())
    with pytest.raises(FeedValidationError, match="not a valid zip"):
        parse(b"not a zip")
