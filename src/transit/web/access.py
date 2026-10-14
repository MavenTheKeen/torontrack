"""Station entrances, bike parking and elevator outages, for the map and trip plans.

The entrances and bike parking come from the OpenStreetMap layers that `transit osm-layers`
writes next to the street map (transit/osm_layers.py). Each entrance belongs to the nearest
subway station within ENTRANCE_M. A trip plan gets, for each subway ride, the entrance to
use and the exit to take: the one closest to the walking route between WALK_FROM_M and
WALK_TO_M from the station, which is the way the planner's walk actually goes (the first
metres are skipped because the walk starts at the platform, under the station). Step-free
trips only consider step-free entrances. Rides that start or end at a station with an
elevator or escalator out of service carry those alerts, and step-free trips without any
are listed first.
"""

import json
import math
import re
from pathlib import Path

ENTRANCE_M = 350
BIKE_PARKING_M = 250
WALK_FROM_M = 40
WALK_TO_M = 400
SAMPLE_M = 10
# A walk shorter than this between a subway ride and the next one stays inside the
# station (a bus bay or another line), so no entrance or exit is suggested for it.
TRANSFER_WALK_M = 120
COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]


def metres(a, b) -> float:
    """Distance between two (lon, lat) points; exact enough within a few km."""
    dx = (b[0] - a[0]) * math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot(dx, b[1] - a[1]) * 111_195


def bearing_name(origin, to) -> str:
    dx = (to[0] - origin[0]) * math.cos(math.radians(origin[1]))
    angle = math.degrees(math.atan2(dx, to[1] - origin[1])) % 360
    return COMPASS[round(angle / 45) % 8]


class Layers:
    """The OpenStreetMap layer files, reloaded when they are rebuilt."""

    def __init__(self, map_dir: str | None):
        self.dir = Path(map_dir) if map_dir else None
        self.loaded: dict[str, tuple] = {}

    def _mtime(self, name: str) -> float | None:
        try:
            return (self.dir / name).stat().st_mtime if self.dir else None
        except OSError:
            return None

    def features(self, name: str) -> list[dict]:
        """The file's features; none while it is missing. A file that cannot be read (say,
        mid-rebuild) keeps the last good copy."""
        mtime = self._mtime(name)
        cached = self.loaded.get(name)
        if mtime is None:
            return []
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            features = json.loads((self.dir / name).read_text(encoding="utf-8"))["features"]
        except (OSError, ValueError, KeyError):
            return cached[1] if cached else []
        self.loaded[name] = (mtime, features)
        return features

    def by_station(self, stations: list[dict], version: str) -> tuple[dict, dict]:
        """(entrances, bike parking) by station key, recomputed only when the schedule
        version or a layer file changes."""
        key = (version, self._mtime("entrances.geojson"), self._mtime("bike-parking.geojson"))
        entrances = self.features("entrances.geojson")
        racks = self.features("bike-parking.geojson")
        if self.loaded.get("_stations", (None,))[0] != key:
            self.loaded["_stations"] = (
                key,
                (station_entrances(stations, entrances), station_bike_parking(stations, racks)),
            )
        return self.loaded["_stations"][1]


def station_entrances(stations: list[dict], features: list[dict]) -> dict[str, list[dict]]:
    """{station key: entrances}, each entrance joined to the nearest station in reach."""
    out: dict[str, list[dict]] = {}
    for f in features:
        point = f["geometry"]["coordinates"]
        best = (ENTRANCE_M, None)
        for s in stations:
            m = metres((s["lon"], s["lat"]), point)
            if m < best[0]:
                best = (m, s)
        if best[1] is None:
            continue
        s, p = best[1], f["properties"]
        centre = (s["lon"], s["lat"])
        out.setdefault(s["key"], []).append(
            {
                "id": p["id"],
                "name": p.get("name") or f"{bearing_name(centre, point).capitalize()} entrance",
                "where": p.get("where"),
                "point": point,
                "step_free": p.get("wheelchair") == "yes",
                "no_step_free": p.get("wheelchair") == "no",
                "automatic": bool(p.get("automatic")),
                "exit_only": bool(p.get("exit_only")),
            }
        )
    for entrances in out.values():
        entrances.sort(key=lambda e: e["name"])
    return out


def _name_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def station_outages(stations: list[dict], alerts: list[dict]) -> dict[str, list[str]]:
    """{station key: elevator and escalator alert headers}, matched through each station's
    platforms. TTC sometimes lists another station's platforms on an alert (a Bloor-Yonge
    escalator also tagged with Sheppard-Yonge's), so when the header starts with the name
    of one of the matched stations ("Bloor-Yonge: Escalator ..."), only that one keeps it."""
    out: dict[str, list[str]] = {}
    for a in alerts:
        stops = set(a["stop_ids"])
        matched = [s for s in stations if stops & set(s.get("stop_ids", []))]
        prefix = _name_key(a["header"].split(":", 1)[0]) if ":" in a["header"] else ""
        named = [s for s in matched if _name_key(s["name"]) == prefix]
        for s in named or matched:
            out.setdefault(s["key"], []).append(a["header"])
    return out


def station_bike_parking(stations: list[dict], features: list[dict]) -> dict[str, dict]:
    """{station key: {"places", "spaces", "covered"}} for racks within BIKE_PARKING_M."""
    out = {}
    for s in stations:
        centre = (s["lon"], s["lat"])
        near = [
            f["properties"]
            for f in features
            if metres(centre, f["geometry"]["coordinates"]) <= BIKE_PARKING_M
        ]
        if near:
            out[s["key"]] = {
                "places": len(near),
                "spaces": sum(p.get("capacity") or 0 for p in near),
                "covered": sum(1 for p in near if p.get("covered")),
            }
    return out


def route_near_station(path: list, from_end: bool = False) -> list:
    """Points every SAMPLE_M along a walk path, from WALK_FROM_M to WALK_TO_M away from the
    station; the path's far end if the walk is shorter. `from_end` walks it backwards, for a
    walk that ends at the station."""
    points = list(reversed(path)) if from_end else list(path)
    out, walked = [], 0.0
    for a, b in zip(points, points[1:], strict=False):
        step = metres(a, b)
        n = max(1, math.ceil(step / SAMPLE_M))
        for k in range(n):
            at = walked + step * k / n
            if WALK_FROM_M <= at <= WALK_TO_M:
                t = k / n
                out.append([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t])
        walked += step
    return out or points[-1:]


def choose(entrances: list[dict], route: list, step_free: bool, leaving: bool) -> dict | None:
    """The entrance closest to the walking route, of those usable this way; step-free ones
    only if asked."""
    usable = [e for e in entrances if leaving or not e["exit_only"]]
    if step_free:
        usable = [e for e in usable if e["step_free"]]
    if not usable or not route:
        return None
    return min(usable, key=lambda e: min(metres(e["point"], p) for p in route))


def _walk(leg: dict | None, first_or_last: bool) -> bool:
    return bool(leg and not leg["network"] and (first_or_last or leg["metres"] >= TRANSFER_WALK_M))


def annotate(
    trips: list[dict],
    station_of_stop: dict[str, dict],
    entrances: dict[str, list[dict]],
    outages: dict[str, list[str]],
    step_free: bool,
) -> list[dict]:
    """Add each subway ride's entrance, exit and elevator outages; returns the trips, with
    those without outages first for step-free trips."""
    for trip in trips:
        legs = trip["legs"]
        seen = {}
        for i, leg in enumerate(legs):
            if leg["mode"] != "subway" or leg["network"] != "ttc":
                continue
            board = station_of_stop.get(leg.get("from_stop_id") or "")
            alight = station_of_stop.get(leg.get("to_stop_id") or "")
            before = legs[i - 1] if i > 0 else None
            after = legs[i + 1] if i + 1 < len(legs) else None
            if board and _walk(before, i == 1):
                route = route_near_station(before["path"], from_end=True)
                leg["entrance"] = choose(entrances.get(board["key"], []), route, step_free, False)
            if alight and _walk(after, i + 2 == len(legs)):
                route = route_near_station(after["path"])
                leg["exit"] = choose(entrances.get(alight["key"], []), route, step_free, True)
            leg["outages"] = []
            for s in (board, alight):
                if s and outages.get(s["key"]):
                    item = {"station": s["name"], "alerts": outages[s["key"]]}
                    leg["outages"].append(item)
                    seen[s["key"]] = item
        trip["outages"] = list(seen.values())
    if step_free:
        trips.sort(key=lambda t: bool(t["outages"]))
    return trips
