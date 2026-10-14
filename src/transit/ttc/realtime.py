"""Decode and validate GTFS-Realtime subway trip updates and alerts.

Pure functions: no HTTP or database access. Each record is either accepted, accepted with
flags (a known source quirk), or rejected; every flag/rejection is returned as an issue so
the caller can log it to ops.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2

from transit.bikeshare.parsing import FeedValidationError

TORONTO = ZoneInfo("America/Toronto")
# Subway service runs past midnight; arrivals before 04:00 belong to the previous day.
SERVICE_DAY_ROLLOVER = timedelta(hours=4)
PAST_TOLERANCE = timedelta(minutes=30)
FUTURE_TOLERANCE = timedelta(hours=3)
STALE_FEED = timedelta(minutes=5)


@dataclass
class Issue:
    issue: str
    action: str  # "rejected" or "flagged"
    entity_id: str | None = None
    stop_id: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class Snapshot:
    feed_timestamp: datetime
    message: gtfs_realtime_pb2.FeedMessage


def decode(payload: bytes) -> Snapshot:
    message = gtfs_realtime_pb2.FeedMessage()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise FeedValidationError("Payload is not a GTFS-Realtime FeedMessage") from exc
    if not message.header.gtfs_realtime_version:
        raise FeedValidationError("GTFS-Realtime header has no version")
    if not message.header.timestamp:
        raise FeedValidationError("GTFS-Realtime header has no timestamp")
    if message.header.incrementality != gtfs_realtime_pb2.FeedHeader.FULL_DATASET:
        raise FeedValidationError("Only FULL_DATASET feeds are supported")
    return Snapshot(datetime.fromtimestamp(message.header.timestamp, UTC), message)


def service_date(arrival: datetime):
    return (arrival.astimezone(TORONTO) - SERVICE_DAY_ROLLOVER).date()


def feed_issues(snapshot: Snapshot, fetched_at: datetime) -> list[Issue]:
    age = fetched_at - snapshot.feed_timestamp
    if age > STALE_FEED or age < -STALE_FEED:
        return [
            Issue(
                "stale_feed_timestamp",
                "flagged",
                detail={"age_seconds": int(age.total_seconds())},
            )
        ]
    return []


def parse_trip_updates(snapshot: Snapshot, stops: dict[str, dict]):
    """Return (predictions, issues). `stops` maps stop_id to route/direction/stop_order.

    The train identity is the vehicle label: TTC reassigns trip_ids to the same train
    from one poll to the next, while labels are stable and unique per snapshot.
    """
    predictions, issues = [], []
    seen_trips, seen_trains = set(), set()
    for entity in snapshot.message.entity:
        if not entity.HasField("trip_update"):
            continue
        update = entity.trip_update
        trip_id = update.trip.trip_id
        route_id = update.trip.route_id
        if not trip_id or trip_id in seen_trips:
            issues.append(Issue("missing_or_duplicate_trip_id", "rejected", entity.id))
            continue
        seen_trips.add(trip_id)
        train_id = update.vehicle.label
        if not train_id:
            issues.append(Issue("missing_vehicle_label", "flagged", entity.id))
            train_id = f"trip-{trip_id}"
        if train_id in seen_trains:
            issues.append(Issue("duplicate_vehicle", "rejected", entity.id))
            continue
        seen_trains.add(train_id)
        _, _, direction_label = entity.id.partition("|")
        rows = []
        for position, stu in enumerate(update.stop_time_update):
            stop = stops.get(stu.stop_id)
            if stop is None:
                issues.append(Issue("unknown_stop", "rejected", entity.id, stu.stop_id))
                continue
            if stop["route_id"] != route_id:
                issues.append(
                    Issue(
                        "stop_route_mismatch",
                        "rejected",
                        entity.id,
                        stu.stop_id,
                        {"trip_route": route_id, "stop_route": stop["route_id"]},
                    )
                )
                continue
            event = stu.arrival if stu.HasField("arrival") else stu.departure
            if not event.time:
                issues.append(Issue("missing_time", "rejected", entity.id, stu.stop_id))
                continue
            arrival = datetime.fromtimestamp(event.time, UTC)
            offset = arrival - snapshot.feed_timestamp
            if offset < -PAST_TOLERANCE or offset > FUTURE_TOLERANCE:
                issues.append(
                    Issue(
                        "implausible_time",
                        "rejected",
                        entity.id,
                        stu.stop_id,
                        {"offset_seconds": int(offset.total_seconds())},
                    )
                )
                continue
            rows.append(
                {
                    "position": position,
                    "train_id": train_id,
                    "trip_id": trip_id,
                    "route_id": route_id,
                    "direction_id": stop["direction_id"],
                    "direction_label": direction_label or None,
                    "stop_id": stu.stop_id,
                    "stop_sequence": stu.stop_sequence or None,
                    "stop_order": stop["stop_order"],
                    "predicted_arrival": arrival,
                }
            )
        predictions.extend(_validate_trip(entity.id, rows, issues))
    return predictions, issues


def _validate_trip(entity_id, rows, issues):
    if not rows:
        return []
    directions = {r["direction_id"] for r in rows}
    if len(directions) > 1:
        # Platforms from both directions in one trip: keep the majority direction.
        counts = {d: sum(r["direction_id"] == d for r in rows) for d in directions}
        keep = max(sorted(counts), key=counts.get)
        for r in rows:
            if r["direction_id"] != keep:
                issues.append(Issue("direction_conflict", "rejected", entity_id, r["stop_id"]))
        rows = [r for r in rows if r["direction_id"] == keep]
    unique = {}
    for r in rows:
        if r["stop_id"] in unique:
            issues.append(Issue("duplicate_stop", "rejected", entity_id, r["stop_id"]))
        else:
            unique[r["stop_id"]] = r
    rows = list(unique.values())
    sequences = [r["stop_sequence"] for r in rows if r["stop_sequence"] is not None]
    if sequences != sorted(sequences) or len(set(sequences)) != len(sequences):
        issues.append(Issue("out_of_order_stop_sequence", "flagged", entity_id))
    by_route_order = sorted(rows, key=lambda r: r["stop_order"])
    if [r["stop_id"] for r in by_route_order] != [r["stop_id"] for r in rows]:
        issues.append(Issue("feed_order_differs_from_line_order", "flagged", entity_id))
    for earlier, later in zip(by_route_order, by_route_order[1:], strict=False):
        if later["predicted_arrival"] < earlier["predicted_arrival"]:
            issues.append(Issue("arrival_time_decreasing", "flagged", entity_id, later["stop_id"]))
        elif later["predicted_arrival"] == earlier["predicted_arrival"]:
            issues.append(
                Issue("identical_consecutive_arrival", "flagged", entity_id, later["stop_id"])
            )
    return by_route_order


# Line status categories, most severe first.
SEVERITY = ["no_service", "reduced_service", "delays", "modified_service", "accessibility"]
EFFECT_STATUS = {
    "NO_SERVICE": "no_service",
    "REDUCED_SERVICE": "reduced_service",
    "SIGNIFICANT_DELAYS": "delays",
    "DETOUR": "modified_service",
    "MODIFIED_SERVICE": "modified_service",
    "STOP_MOVED": "modified_service",
    "ACCESSIBILITY_ISSUE": "accessibility",
}
TEXT_PATTERNS = [
    (
        "no_service",
        re.compile(
            r"\bno (?:subway )?service\b|\bnot stopping\b|\bbypassing\b|\bsuspended\b"
            r"|\bstations? (?:is |are |will be )?closed\b",
            re.I,
        ),
    ),
    ("reduced_service", re.compile(r"\breduced service\b|\bless frequent\b", re.I)),
    ("delays", re.compile(r"\bdelays?\b|\bdelayed\b|\bholding\b|\bslower\b", re.I)),
    ("accessibility", re.compile(r"\belevator\b|\bescalator\b", re.I)),
]


# Advance notices of planned work are published days ahead with an active period covering
# the whole notice window, e.g. "There will be no subway service ... nightly ...".
ADVANCE_NOTICE = re.compile(r"\bthere will be\b|\bwill be closed\b|\bwill not (?:run|stop)\b", re.I)


def classify_alert(effect: str, text: str) -> dict:
    """TTC alerts have no severity, and effect can contradict the text, so use both."""
    effect_status = EFFECT_STATUS.get(effect)
    text_status = next((status for status, pattern in TEXT_PATTERNS if pattern.search(text)), None)
    candidates = [s for s in (effect_status, text_status) if s]
    derived = min(candidates, key=SEVERITY.index) if candidates else "other"
    mismatch = bool(
        text_status
        and effect_status
        and text_status != effect_status
        and "accessibility" not in (text_status, effect_status)
    )
    return {
        "effect_status": effect_status,
        "text_status": text_status,
        "derived_status": derived,
        "status_mismatch": mismatch,
        "advance_notice": bool(ADVANCE_NOTICE.search(text)),
    }


def _text(translated) -> str | None:
    for translation in translated.translation:
        if translation.language in ("", "en"):
            return " ".join(translation.text.split())
    return None


def parse_alerts(snapshot: Snapshot) -> tuple[list[dict], list[Issue]]:
    alerts, issues = [], []
    seen = set()
    for entity in snapshot.message.entity:
        if not entity.HasField("alert"):
            continue
        if not entity.id or entity.id in seen:
            issues.append(Issue("missing_or_duplicate_alert_id", "rejected", entity.id))
            continue
        seen.add(entity.id)
        alert = entity.alert
        header = _text(alert.header_text)
        if not header:
            issues.append(Issue("alert_without_text", "rejected", entity.id))
            continue
        description = _text(alert.description_text)
        content = {
            "cause": gtfs_realtime_pb2.Alert.Cause.Name(alert.cause),
            "effect": gtfs_realtime_pb2.Alert.Effect.Name(alert.effect),
            "header_text": header,
            "description_text": description,
            "url": _text(alert.url),
            "active_periods": [
                {"start": p.start or None, "end": p.end or None} for p in alert.active_period
            ],
            "informed_entities": [
                {"route_id": e.route_id or None, "stop_id": e.stop_id or None}
                for e in alert.informed_entity
            ],
        }
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        status = classify_alert(content["effect"], f"{header} {description or ''}")
        if status["status_mismatch"]:
            issues.append(
                Issue(
                    "alert_effect_text_mismatch",
                    "flagged",
                    entity.id,
                    detail={
                        "effect": content["effect"],
                        "text_status": status["text_status"],
                    },
                )
            )
        alerts.append(
            {
                "alert_id": entity.id,
                "version_hash": hashlib.sha256(canonical.encode()).hexdigest(),
                **content,
                **status,
            }
        )
    return alerts, issues
