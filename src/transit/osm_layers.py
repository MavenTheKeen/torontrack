"""Subway entrances, parking and bike parking from OpenStreetMap, as map layers.

Read from BBBike's daily Toronto extract (the trip planner's street source, docs/planner.md)
and written next to the street map:

- entrances.geojson: subway station entrances (railway=subway_entrance), with their name,
  whether they are step-free (wheelchair), automatic (no collector, PRESTO only) or exit
  only. The web service assigns each to its nearest station.
- parking.geojson: car parks open to the public, and the entrances mapped for them
  (amenity=parking_entrance). Customer-only, private, permit and on-street parking are left
  out, as are unnamed lots with no fee, operator or public access tag, which are mostly
  building lots.
- bike-parking.geojson: bicycle racks, shelters and lockers that are not private;
- places.sqlite: the trip planner's address and place search (transit/place_index.py).

Each place is matched to the nearest named street within STREET_M and the side of it that
it is on ("north side of Charles Street East"), from the perpendicular between the street
and the place, rounded to a compass point. A place on the street line itself has no side.
Car parks mapped as multipolygon relations (a handful) are skipped.

© OpenStreetMap contributors, available under the Open Database Licence.
"""

import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import osmium

from transit.http import FeedClient, FetchError
from transit.planner import OSM_HOST, OSM_URL

FILES = {
    "entrances": "entrances.geojson",
    "parking": "parking.geojson",
    "bike_parking": "bike-parking.geojson",
}
STREET_M = 80  # A place further than this from any named street gets no street.
ON_STREET_M = 4  # Closer than this to the street line, a place has no side.
LOT_ENTRANCE_M = 40  # An entrance belongs to a car park whose outline is this close.
STREET_TYPES = {
    "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
    "living_street", "pedestrian", "service",
}  # fmt: skip
CLOSED_ACCESS = {
    "private", "no", "customers", "permit", "employees", "residents", "delivery", "staff",
    "students", "emergency", "military", "agricultural", "forestry",
}  # fmt: skip
OPEN_ACCESS = {"yes", "permissive", "public", "destination"}
STREET_PARKING = {
    "street_side", "lane", "on_kerb", "half_on_kerb", "shoulder", "layby", "garage_boxes",
    "carports", "sheds",
}  # fmt: skip
PARKING_KINDS = {"surface", "underground", "multi-storey", "rooftop"}
CELL = 0.002  # Street index cell, in degrees (about 220 m by 160 m in Toronto).
M_PER_DEG_LAT = 111_195.0


def short_street(name: str) -> str:
    """'Bloor Street East' -> 'Bloor St E', as on street signs."""
    words = name.split()
    short = {"Street": "St", "Avenue": "Ave", "Road": "Rd", "Boulevard": "Blvd", "Drive": "Dr"}
    ends = {"East": "E", "West": "W", "North": "N", "South": "S"}
    if len(words) > 2 and words[-1] in ends and words[-2] in short:
        return " ".join([*words[:-2], short[words[-2]], ends[words[-1]]])
    if len(words) > 1 and words[-1] in short:
        return " ".join([*words[:-1], short[words[-1]]])
    return name


def _xy(lon: float, lat: float, origin: tuple[float, float]) -> tuple[float, float]:
    """Metres east and north of `origin` (lon, lat); exact enough within a few hundred m."""
    return (
        (lon - origin[0]) * M_PER_DEG_LAT * math.cos(math.radians(origin[1])),
        (lat - origin[1]) * M_PER_DEG_LAT,
    )


def compass(dx: float, dy: float) -> str:
    """The nearest of north, east, south and west to a direction in metres east and north."""
    if abs(dy) >= abs(dx):
        return "north" if dy > 0 else "south"
    return "east" if dx > 0 else "west"


def street_side(place: tuple[float, float], a, b) -> tuple[float, str | None]:
    """(metres from segment a-b, side of it) for a place; all three are (lon, lat)."""
    ax, ay = _xy(*a, place)
    bx, by = _xy(*b, place)
    sx, sy = bx - ax, by - ay
    length2 = sx * sx + sy * sy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * sx + ay * sy) / length2))
    # Vector from the nearest point of the segment to the place (which is the origin).
    vx, vy = -(ax + t * sx), -(ay + t * sy)
    distance = math.hypot(vx, vy)
    if distance < ON_STREET_M or length2 == 0:
        return distance, None
    # The side is the segment's normal pointing at the place, so a place past the end of a
    # segment still gets the side across the street rather than "east" of its end.
    nx, ny = -sy, sx
    if nx * vx + ny * vy < 0:
        nx, ny = -nx, -ny
    return distance, compass(nx, ny)


class Streets:
    """Named street segments in a grid, for finding the street a place is on."""

    def __init__(self):
        self.cells: dict[tuple[int, int], list] = {}

    def add(self, name: str, coords: list[tuple[float, float]]) -> None:
        for a, b in zip(coords, coords[1:], strict=False):
            x0, x1 = sorted((int(a[0] // CELL), int(b[0] // CELL)))
            y0, y1 = sorted((int(a[1] // CELL), int(b[1] // CELL)))
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    self.cells.setdefault((x, y), []).append((name, a, b))

    def nearest(self, place: tuple[float, float], lot: bool = False) -> dict:
        """{"street", "side", "where"}: the street, the side of it the place is on, and that
        as text ("north side of Bloor St E", "on Yonge St"); empty if no street is close. A
        car park's centre says little about which street it opens onto, so a car park is
        only "near" its street."""
        cx, cy = int(place[0] // CELL), int(place[1] // CELL)
        best = (STREET_M, None, None)
        for x in (cx - 1, cx, cx + 1):
            for y in (cy - 1, cy, cy + 1):
                for name, a, b in self.cells.get((x, y), ()):
                    distance, side = street_side(place, a, b)
                    if distance < best[0]:
                        best = (distance, name, side)
        distance, name, side = best
        if not name:
            return {}
        street = short_street(name)
        where = f"near {street}" if lot else f"{side} side of {street}" if side else f"on {street}"
        return {"street": name, "side": None if lot else side, "where": where}


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fee(value) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() not in ("no", "free")


def _yes(value) -> bool:
    return (value or "").strip().lower() == "yes"


def _point(lon: float, lat: float) -> list[float]:
    return [round(lon, 6), round(lat, 6)]


def _feature(coord, props: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": coord},
        "properties": {k: v for k, v in props.items() if v is not None and v != ""},
    }


def public_parking(tags) -> bool:
    if tags.get("amenity") != "parking":
        return False
    access = (tags.get("access") or "").lower()
    kind = (tags.get("parking") or "surface").lower()
    if access in CLOSED_ACCESS or kind in STREET_PARKING:
        return False
    return bool(
        access in OPEN_ACCESS
        or tags.get("fee")
        or tags.get("name")
        or tags.get("operator")
        or kind in ("underground", "multi-storey", "rooftop")
        or _yes(tags.get("park_ride"))
    )


def _lot(tags) -> dict:
    kind = (tags.get("parking") or "surface").lower()
    return {
        "name": tags.get("name"),
        "operator": tags.get("operator"),
        "kind": kind if kind in PARKING_KINDS else "surface",
        "fee": _fee(tags.get("fee")),
        "capacity": _int(tags.get("capacity")),
        "maxheight": tags.get("maxheight"),
        "hours": tags.get("opening_hours"),
        "park_ride": _yes(tags.get("park_ride")) or None,
    }


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.streets = Streets()
        self.entrances, self.lots, self.lot_entrances, self.racks = [], [], [], []
        self.skipped = 0

    def node(self, n):
        tags = n.tags
        if not tags:
            return
        where = (n.location.lon, n.location.lat)
        if tags.get("railway") == "subway_entrance":
            self.entrances.append(
                (
                    where,
                    {
                        "id": f"n{n.id}",
                        "name": tags.get("name"),
                        "ref": tags.get("ref"),
                        "wheelchair": tags.get("wheelchair"),
                        "automatic": _yes(tags.get("automatic"))
                        or _yes(tags.get("automated"))
                        or "automatic" in (tags.get("note") or "").lower()
                        or None,
                        "exit_only": tags.get("entrance") == "exit" or None,
                    },
                )
            )
        elif tags.get("amenity") == "parking_entrance":
            if (tags.get("access") or "").lower() not in CLOSED_ACCESS:
                self.lot_entrances.append((where, f"n{n.id}", dict(tags)))
        elif public_parking(tags):
            self.lots.append((where, None, f"n{n.id}", _lot(tags)))
        elif tags.get("amenity") == "bicycle_parking":
            self._rack(where, f"n{n.id}", tags)

    def way(self, w):
        tags = w.tags
        highway, amenity = tags.get("highway"), tags.get("amenity")
        if not (
            (highway in STREET_TYPES and tags.get("name"))
            or amenity == "bicycle_parking"
            or public_parking(tags)
        ):
            return
        try:
            coords = [(node.location.lon, node.location.lat) for node in w.nodes]
        except osmium.InvalidLocationError:
            self.skipped += 1  # Partly outside the extract.
            return
        if len(coords) < 2:
            return
        if highway in STREET_TYPES and tags.get("name"):
            self.streets.add(tags["name"], coords)
            return
        ring = coords[:-1] if coords[0] == coords[-1] and len(coords) > 3 else coords
        centre = (sum(c[0] for c in ring) / len(ring), sum(c[1] for c in ring) / len(ring))
        if amenity == "bicycle_parking":
            self._rack(centre, f"w{w.id}", tags)
        else:
            self.lots.append((centre, ring, f"w{w.id}", _lot(tags)))

    def _rack(self, where, osm_id, tags):
        if (tags.get("access") or "").lower() in ("private", "no"):
            return
        self.racks.append(
            (
                where,
                {
                    "id": osm_id,
                    "kind": tags.get("bicycle_parking"),
                    "capacity": _int(tags.get("capacity")),
                    "covered": _yes(tags.get("covered")) or None,
                    "fee": _fee(tags.get("fee")),
                    "name": tags.get("name"),
                    "operator": tags.get("operator"),
                },
            )
        )


def _ring_distance(place, ring) -> float:
    """Metres from a place to a car park outline (0 inside it)."""
    if len(ring) < 3:
        return min(math.hypot(*_xy(*c, place)) for c in ring)
    pts = [_xy(*c, place) for c in ring]
    inside = False
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1], strict=True):
        if (y0 > 0) != (y1 > 0) and 0 < x0 + (0 - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    if inside:
        return 0.0
    return min(street_side(place, a, b)[0] for a, b in zip(ring, ring[1:] + ring[:1], strict=True))


def _parking(c: Collector) -> tuple[list[dict], dict]:
    """Car parks and their entrances. An entrance joins the car park whose outline is within
    LOT_ENTRANCE_M; one with none becomes a car park of its own at the entrance."""
    lots = {osm_id: (where, ring, props) for where, ring, osm_id, props in c.lots}
    entrances: dict[str, list] = {}
    standalone = 0
    for where, osm_id, tags in c.lot_entrances:
        near = []
        for lot_id, (centre, ring, _) in lots.items():
            if math.hypot(*_xy(*centre, where)) > 1500:
                continue
            distance = _ring_distance(where, ring) if ring else math.hypot(*_xy(*centre, where))
            if distance <= LOT_ENTRANCE_M:
                near.append((distance, lot_id))
        if near:
            entrances.setdefault(min(near)[1], []).append((where, osm_id))
        else:
            props = _lot({"parking": "underground", **tags, "amenity": "parking"})
            lots[osm_id] = (where, None, props)
            entrances.setdefault(osm_id, []).append((where, osm_id))
            standalone += 1
    features = []
    for lot_id, (centre, _, props) in lots.items():
        doors = entrances.get(lot_id, [])
        features.append(
            _feature(
                _point(*centre),
                {"id": lot_id, "role": "lot", "entrances": len(doors) or None, **props}
                | c.streets.nearest(centre, lot=True),
            )
        )
        for where, osm_id in doors:
            features.append(
                _feature(
                    _point(*where),
                    {"id": osm_id, "role": "entrance", "lot": lot_id} | c.streets.nearest(where),
                )
            )
    counts = {
        "car_parks": len(lots),
        "entrances": sum(len(v) for v in entrances.values()),
        "entrances_without_car_park": standalone,
    }
    return features, counts


def layers(path: str | os.PathLike) -> tuple[dict[str, dict], dict]:
    """Read an OpenStreetMap file; ({key: GeoJSON}, counts)."""
    c = Collector()
    c.apply_file(str(path), locations=True)
    entrances = [
        _feature(_point(*where), props | c.streets.nearest(where)) for where, props in c.entrances
    ]
    parking, parking_counts = _parking(c)
    racks = [_feature(_point(*where), props) for where, props in c.racks]
    if not entrances:
        raise FetchError("OpenStreetMap extract has no subway entrances")
    out = {
        "entrances": entrances,
        "parking": parking,
        "bike_parking": racks,
    }
    counts = {
        "subway_entrances": len(entrances),
        **parking_counts,
        "bike_parking": len(racks),
        "bike_parking_spaces": sum(p["properties"].get("capacity", 0) for p in racks),
        "ways_outside_extract": c.skipped,
    }
    return {k: {"type": "FeatureCollection", "features": v} for k, v in out.items()}, counts


def _write(out: Path, name: str, value) -> None:
    handle, tmp = tempfile.mkstemp(dir=out, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(value, f, separators=(",", ":"))
        os.chmod(tmp, 0o644)
        os.replace(tmp, out / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build(out_dir: str, source: str | None = None, client: FeedClient | None = None) -> dict:
    """Write the three layers into `out_dir`, from `source` (a local .osm.pbf, such as the
    trip planner's copy) or a fresh download of the Toronto extract."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        if source is None:
            own = client is None
            client = client or FeedClient()
            try:
                with tempfile.NamedTemporaryFile(dir=out, suffix=".osm.pbf", delete=False) as f:
                    tmp = f.name
                    client.fetch_bytes(OSM_URL, host=OSM_HOST, max_bytes=400 << 20, sink=f)
            finally:
                if own:
                    client.close()
        geojson, counts = layers(tmp or source)
        for key, name in FILES.items():
            _write(out, name, geojson[key])
        # The address search reads the same file, so it is built while it is here.
        from transit import place_index

        index = place_index.build_index(tmp or source, out)
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    result = {
        "built_at": datetime.now(UTC).isoformat(),
        "source": source or OSM_URL,
        "files": {name: (out / name).stat().st_size for name in [*FILES.values(), index["file"]]},
        "counts": counts | {"search": index["counts"]},
    }
    (out / "osm-layers.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
