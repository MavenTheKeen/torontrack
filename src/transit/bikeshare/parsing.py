"""The GBFS 3.0 fields we use. Unknown fields stay in the raw JSONB payload.

Fail a collection on malformed required fields instead of silently dropping stations.
Optional disabled counts and capacity are NULL, never guessed to be zero.
"""

import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse


class FeedValidationError(ValueError):
    pass


def timestamp(value: Any, field: str = "timestamp") -> datetime:
    if not isinstance(value, str):
        raise FeedValidationError(f"{field}: expected RFC3339 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeedValidationError(f"{field}: invalid timestamp") from exc
    if result.tzinfo is None:
        raise FeedValidationError(f"{field}: timezone required")
    return result.astimezone(UTC)


def object_value(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise FeedValidationError(f"{field}: expected object")
    return value


def envelope(payload: Any) -> dict:
    root = object_value(payload, "root")
    if root.get("version") != "3.0":
        raise FeedValidationError("Only GBFS 3.0 is supported; configure its discovery URL")
    timestamp(root.get("last_updated"), "last_updated")
    count(root, "ttl")
    return object_value(root.get("data"), "data")


def count(row: dict, key: str, optional: bool = False) -> int | None:
    value = row.get(key)
    if value is None and optional:
        return None
    if type(value) is not int or value < 0:
        raise FeedValidationError(f"{key}: expected nonnegative integer")
    return value


def identifier(row: dict) -> str:
    value = row.get("station_id")
    if not isinstance(value, str) or not value.strip():
        raise FeedValidationError("station_id: expected nonempty string")
    return value


def flag(row: dict, key: str) -> bool:
    if type(row.get(key)) is not bool:
        raise FeedValidationError(f"{key}: expected boolean")
    return row[key]


def coordinate(row: dict, key: str, limit: int) -> float:
    value = row.get(key)
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > limit:
        raise FeedValidationError(f"{key}: invalid coordinate")
    return float(value)


def station_rows(payload: Any) -> list[dict]:
    rows = envelope(payload).get("stations")
    if not isinstance(rows, list) or not rows:
        raise FeedValidationError("stations: expected nonempty list; empty feed is not success")
    seen = set()
    for row in rows:
        object_value(row, "station")
        station_id = identifier(row)
        if station_id in seen:
            raise FeedValidationError(f"Duplicate station_id: {station_id}")
        seen.add(station_id)
    return rows


def discover(payload: Any) -> dict[str, str]:
    feeds = envelope(payload).get("feeds")
    if not isinstance(feeds, list):
        raise FeedValidationError("data.feeds: expected list")
    urls = {}
    for feed in feeds:
        object_value(feed, "feed")
        name, url = feed.get("name"), feed.get("url")
        if not isinstance(name, str) or not isinstance(url, str):
            raise FeedValidationError("Feed name and URL must be strings")
        if urlparse(url).scheme != "https" or not urlparse(url).hostname:
            raise FeedValidationError("Feed URL must use HTTPS")
        if name in urls:
            raise FeedValidationError(f"Duplicate feed: {name}")
        urls[name] = url
    if not {"station_information", "station_status"} <= urls.keys():
        raise FeedValidationError("Discovery missing station_information or station_status")
    return urls


def parse_information(payload: Any) -> list[dict]:
    result = []
    for row in station_rows(payload):
        names = row.get("name")
        if not isinstance(names, list) or not names:
            raise FeedValidationError("name: expected nonempty translated string list")
        for name in names:
            if (
                not isinstance(name, dict)
                or not isinstance(name.get("text"), str)
                or not name["text"].strip()
                or not isinstance(name.get("language"), str)
            ):
                raise FeedValidationError("name: malformed translation")
        name = next((n["text"] for n in names if n["language"] == "en"), names[0]["text"])
        result.append(
            {
                "station_id": identifier(row),
                "name": name,
                "lat": coordinate(row, "lat", 90),
                "lon": coordinate(row, "lon", 180),
                "capacity": count(row, "capacity", optional=True),
            }
        )
    return result


def parse_status(payload: Any) -> list[dict]:
    return [
        {
            "station_id": identifier(row),
            "num_bikes_available": count(row, "num_vehicles_available"),
            "num_docks_available": count(row, "num_docks_available"),
            "num_bikes_disabled": count(row, "num_vehicles_disabled", optional=True),
            "num_docks_disabled": count(row, "num_docks_disabled", optional=True),
            "is_installed": flag(row, "is_installed"),
            "is_renting": flag(row, "is_renting"),
            "is_returning": flag(row, "is_returning"),
            "station_reported_at": timestamp(row.get("last_reported"), "last_reported"),
        }
        for row in station_rows(payload)
    ]
