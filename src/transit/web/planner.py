"""Trip plans from the self-hosted OpenTripPlanner, shaped for the map.

OpenTripPlanner (docs/planner.md) routes on the TTC and regional train and bus schedules and
OpenStreetMap streets, and Bike Share from its live station feed. Plans use published
timetables only, not live predictions. Regional names drop the operator's brand
('Hamilton GO' -> 'Hamilton'), as the rest of the site does.

A trip can leave out any of MODES. Subway, streetcar and regional train are modes of their
own in the schedules, but TTC and regional buses are both "BUS", so leaving out one of the
two excludes that network's bus routes by ID (asked of the planner once, then cached).
"""

import re
import time
from datetime import datetime

import httpx

from transit.agencies import route_number, route_title, towards
from transit.planner import EXTENT
from transit.regional import clean_name

QUERY = """
query Plan($origin: PlanLabeledLocationInput!, $destination: PlanLabeledLocationInput!,
           $dateTime: PlanDateTimeInput!, $preferences: PlanPreferencesInput,
           $modes: PlanModesInput) {
  planConnection(origin: $origin, destination: $destination, dateTime: $dateTime, first: 4,
                 preferences: $preferences, modes: $modes) {
    routingErrors { code }
    edges { node {
      start end numberOfTransfers walkDistance
      legs {
        mode duration distance headsign rentedBike
        start { scheduledTime } end { scheduledTime }
        from { name lat lon stop { gtfsId } vehicleRentalStation { name } }
        to { name lat lon stop { gtfsId } vehicleRentalStation { name } }
        route { gtfsId shortName longName color }
        legGeometry { points }
        intermediatePlaces { name lat lon stop { gtfsId } arrival { scheduledTime } }
        steps { distance relativeDirection streetName bogusName lat lon }
      }
    } }
  }
}
"""
TRANSIT_MODES = {"SUBWAY", "TRAM", "BUS", "RAIL", "FERRY"}
# What a visitor can switch on or off, and the planner's transit mode for each.
MODES = {
    "subway": "SUBWAY",
    "tram": "TRAM",
    "bus": "BUS",
    "rail": "RAIL",
    "regional_bus": "BUS",
    "bike_share": None,
}
DEFAULT_MODES = frozenset(MODES) - {"bike_share"}
BUS_ROUTES_SECONDS = 6 * 3600
_bus_routes: dict[str, tuple[float, dict]] = {}


class PlannerError(RuntimeError):
    pass


def inside(lat: float, lon: float) -> bool:
    west, south, east, north = EXTENT
    return south <= lat <= north and west <= lon <= east


def decode_polyline(points: str) -> list[list[float]]:
    """Google encoded polyline (precision 5) -> [[lon, lat], ...]."""
    coords, index, lat, lon = [], 0, 0, 0
    while index < len(points):
        deltas = []
        for _ in range(2):
            shift = result = 0
            while True:
                byte = ord(points[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        lat, lon = lat + deltas[0], lon + deltas[1]
        coords.append([round(lon / 1e5, 5), round(lat / 1e5, 5)])
    return coords


def _feed(gtfs_id: str | None) -> str | None:
    return gtfs_id.split(":", 1)[0] if gtfs_id else None


def _network(gtfs_id: str | None) -> str | None:
    """'ttc', 'regional' (GO and UP Express), or a neighbouring agency's key ('br')."""
    feed = _feed(gtfs_id)
    return "regional" if feed == "go" else feed


def _place(place: dict) -> str | None:
    if place.get("vehicleRentalStation"):
        return place["vehicleRentalStation"]["name"]  # A Bike Share station.
    if not place.get("stop"):
        return None  # The origin or destination itself.
    network = _network(place["stop"]["gtfsId"])
    if network == "regional":
        return clean_name(place["name"])
    if network != "ttc":
        return route_title(place["name"])
    # TTC names each subway platform: 'St George Station - Eastbound Platform' -> 'St George
    # Station'. The direction is already in the headsign.
    return re.sub(r"\s+-\s+[^-]*Platform\b.*$", "", place["name"])


def _stop_id(place: dict) -> str | None:
    """The stop's ID within its network's GTFS ('ttc:14229' -> '14229'), for live lookups."""
    stop = place.get("stop")
    return stop["gtfsId"].split(":", 1)[-1] if stop else None


def _step(step: dict) -> dict:
    """A walking instruction. OpenTripPlanner names unnamed ways 'path' or 'steps' and flags
    them as bogus; those get no street name."""
    return {
        "action": (step.get("relativeDirection") or "CONTINUE").lower(),
        "street": None if step.get("bogusName") else step.get("streetName"),
        "metres": round(step.get("distance") or 0),
        "point": [round(step["lon"], 6), round(step["lat"], 6)],
    }


def _headsign(headsign: str | None, network: str | None) -> str | None:
    """Regional 'Kennedy GO 09:01 - Union Station GO 09:20' -> 'Union Station';
    TTC 'South - 904 Sheppard-Kennedy Express towards Kennedy Stn' -> 'Kennedy Stn'."""
    if not headsign:
        return None
    if network not in ("ttc", "regional"):
        return towards(headsign)
    if network == "regional":
        last = headsign.rsplit(" - ", 1)[-1]
        return clean_name(re.sub(r"\s+\d{1,2}:\d{2}$", "", last))
    return headsign.split(" towards ", 1)[-1]


def _leg(leg: dict) -> dict:
    route = leg.get("route") or {}
    transit = leg["mode"] in TRANSIT_MODES
    network = _network(route.get("gtfsId"))
    local = network not in ("ttc", "regional")
    return {
        "mode": leg["mode"].lower(),
        "network": network if transit else None,
        "route": (
            (
                route_number(route["shortName"])
                if local and route.get("shortName")
                else route.get("shortName") or route.get("longName")
            )
            if transit
            else None
        ),
        "route_name": (
            (route_title(route["longName"]) if local else route["longName"])
            if transit and route.get("longName")
            else None
        ),
        "color": f"#{route['color']}" if transit and route.get("color") else None,
        "headsign": _headsign(leg.get("headsign"), network) if transit else None,
        # A ride on a Bike Share bike, from one station to another.
        "rented": bool(leg.get("rentedBike")),
        "from": _place(leg["from"]),
        "to": _place(leg["to"]),
        "from_point": [round(leg["from"]["lon"], 6), round(leg["from"]["lat"], 6)],
        "to_point": [round(leg["to"]["lon"], 6), round(leg["to"]["lat"], 6)],
        "from_stop_id": _stop_id(leg["from"]) if transit else None,
        "to_stop_id": _stop_id(leg["to"]) if transit else None,
        "start": leg["start"]["scheduledTime"],
        "end": leg["end"]["scheduledTime"],
        "seconds": round(leg["duration"]),
        "metres": round(leg["distance"]),
        "path": decode_polyline((leg.get("legGeometry") or {}).get("points") or ""),
        # Stops passed between boarding and getting off, for counting down to the last one.
        "stops": [
            {
                "name": _place(p) or p["name"],
                "point": [round(p["lon"], 6), round(p["lat"], 6)],
                "arrival": (p.get("arrival") or {}).get("scheduledTime"),
            }
            for p in leg.get("intermediatePlaces") or []
        ]
        if transit
        else [],
        "steps": [] if transit else [_step(s) for s in leg.get("steps") or []],
    }


def itineraries(data: dict) -> list[dict]:
    """OpenTripPlanner planConnection response -> itineraries for the map, fastest first."""
    plan = data["planConnection"]
    trips = []
    for edge in plan["edges"]:
        node = edge["node"]
        legs = [_leg(leg) for leg in node["legs"]]
        start, end = datetime.fromisoformat(node["start"]), datetime.fromisoformat(node["end"])
        trips.append(
            {
                "start": node["start"],
                "end": node["end"],
                "seconds": round((end - start).total_seconds()),
                "transfers": node["numberOfTransfers"],
                "walk_metres": round(node["walkDistance"]),
                "legs": legs,
            }
        )
    trips.sort(key=lambda t: (t["end"], t["seconds"]))
    return trips


def bus_routes(client: httpx.Client, base_url: str) -> dict[str, list[str]]:
    """{"ttc": [route IDs], "go": [...], "br": [...]} of bus routes, by feed; cached."""
    cached = _bus_routes.get(base_url)
    if cached and time.monotonic() - cached[0] < BUS_ROUTES_SECONDS:
        return cached[1]
    response = client.post(
        f"{base_url.rstrip('/')}/otp/gtfs/v1",
        json={"query": "{ routes(transportModes: [BUS]) { gtfsId } }"},
    )
    response.raise_for_status()
    routes: dict[str, list[str]] = {"ttc": [], "go": []}
    for r in response.json()["data"]["routes"]:
        routes.setdefault(_feed(r["gtfsId"]), []).append(r["gtfsId"])
    _bus_routes[base_url] = (time.monotonic(), routes)
    return routes


def modes_input(modes: frozenset[str]) -> dict:
    """The planner's modes for a set of MODES. Walking is always allowed."""
    street = ["WALK", "BICYCLE_RENTAL"] if "bike_share" in modes else ["WALK"]
    transit = sorted({MODES[m] for m in modes if MODES.get(m)})
    if not transit:
        return {"direct": street, "directOnly": True}
    return {
        "direct": street,
        "transit": {
            "access": street,
            "egress": street,
            "transfer": ["WALK"],
            "transit": [{"mode": m} for m in transit],
        },
    }


def plan(
    client: httpx.Client,
    base_url: str,
    origin: tuple[float, float],
    to: tuple[float, float],
    when: datetime,
    arrive_by: bool = False,
    step_free: bool = False,
    modes: frozenset[str] = DEFAULT_MODES,
) -> dict:
    """{"itineraries": [...], "errors": [routing error codes]}; PlannerError if it is down.
    Step-free trips use only stations and vehicles the schedules mark wheelchair accessible.
    `modes` is the set of MODES the trip may use."""

    def place(lat_lon):
        return {"location": {"coordinate": {"latitude": lat_lon[0], "longitude": lat_lon[1]}}}

    when_key = "latestArrival" if arrive_by else "earliestDeparture"
    preferences: dict = {"accessibility": {"wheelchair": {"enabled": step_free}}}
    try:
        # Local and regional buses share one mode, so leaving out one excludes its routes.
        if ("bus" in modes) != ("regional_bus" in modes):
            by_feed = bus_routes(client, base_url)
            if "bus" in modes:
                routes = by_feed.get("go", [])
            else:
                routes = [r for feed, ids in by_feed.items() if feed != "go" for r in ids]
            if routes:
                preferences["transit"] = {"filters": [{"exclude": [{"routes": routes}]}]}
        response = client.post(
            f"{base_url.rstrip('/')}/otp/gtfs/v1",
            json={
                "query": QUERY,
                "variables": {
                    "origin": place(origin),
                    "destination": place(to),
                    "dateTime": {when_key: when.isoformat()},
                    "preferences": preferences,
                    "modes": modes_input(modes),
                },
            },
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise PlannerError("Trip planner is unavailable") from exc
    if body.get("errors") or not body.get("data"):
        raise PlannerError("Trip planner rejected the request")
    return {
        "itineraries": itineraries(body["data"]),
        "errors": [e["code"] for e in body["data"]["planConnection"]["routingErrors"]],
    }
