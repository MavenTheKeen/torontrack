"""How much earlier to leave to arrive on time about 9 times in 10 (docs/planner.md).

Each TTC ride adds its route's 90th percentile wait and, on buses and streetcars, the time
its ride takes beyond the timetable at the 90th percentile. Rides are not fully independent
and rarely all go badly, so the buffer is halfway between adding them up and their root
sum of squares. A trip with any ride that has no measurements gets no estimate.
"""

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")
# Fewer measured days than this say too little about a route.
MIN_DAYS = 4


def _key(day_kind: str, route: str, direction: int, band: str) -> str:
    return f"{day_kind}|{route}|{direction}|{band}"


def load(conn) -> dict:
    """Measured waits and rides by day kind, route, direction and part of the day."""
    rows = conn.execute(
        """SELECT day_kind, route_id, direction_id, band, wait_p90_seconds, ride_ratio_p90
           FROM analytics.ttc_on_time_buffers WHERE days >= %s""",
        (MIN_DAYS,),
    ).fetchall()
    return {
        _key(r["day_kind"], r["route_id"], r["direction_id"], r["band"]): {
            "wait": r["wait_p90_seconds"],
            "ride_ratio": float(r["ride_ratio_p90"]) if r["ride_ratio_p90"] is not None else None,
        }
        for r in rows
    }


def _band(hour: int) -> str:
    if 6 <= hour <= 9:
        return "am"
    if 10 <= hour <= 14:
        return "mid"
    if 15 <= hour <= 18:
        return "pm"
    return "off"


def _ride_seconds(leg: dict, buffers: dict) -> tuple[float, float] | None:
    """The leg's extra wait and extra ride at the 90th percentile; None if not measured."""
    if leg.get("network") != "ttc" or leg.get("direction") is None or not leg.get("start"):
        return None
    start = datetime.fromisoformat(leg["start"]).astimezone(TORONTO)
    # The service day starts at 04:00.
    day_kind = "weekend" if (start - timedelta(hours=4)).weekday() >= 5 else "weekday"
    found = buffers.get(_key(day_kind, leg["route"], leg["direction"], _band(start.hour)))
    if found is None:
        return None
    if leg["mode"] == "subway":
        return found["wait"], 0.0  # Subway ride times are not measured.
    if found["ride_ratio"] is None:
        return None
    return found["wait"], leg["seconds"] * max(found["ride_ratio"] - 1, 0)


def buffer_minutes(trip: dict, buffers: dict) -> int | None:
    parts = []
    transit = [leg for leg in trip["legs"] if leg.get("network")]
    for leg in transit:
        seconds = _ride_seconds(leg, buffers)
        if seconds is None:
            return None
        parts.extend(seconds)
    if not transit:
        return None
    total = sum(parts)
    squares = math.sqrt(sum(p * p for p in parts))
    return math.ceil((total + squares) / 2 / 60)


def annotate(trips: list[dict], buffers: dict) -> None:
    """Adds `on_time` to each trip the measurements cover."""
    for trip in trips:
        minutes = buffer_minutes(trip, buffers)
        if minutes is None:
            continue
        extra = timedelta(minutes=minutes)
        trip["on_time"] = {
            "buffer_minutes": minutes,
            "leave_by": (datetime.fromisoformat(trip["start"]) - extra).isoformat(),
            "arrive_by": (datetime.fromisoformat(trip["end"]) + extra).isoformat(),
        }
