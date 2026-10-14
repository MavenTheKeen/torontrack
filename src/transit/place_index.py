"""Builds places.sqlite, the address and place search (transit/places.py), from an
OpenStreetMap extract. Run by `transit osm-layers`, which reads the same file.

© OpenStreetMap contributors, available under the Open Database Licence.
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import osmium

from transit.osm_layers import short_street
from transit.places import FILE_NAME, metres, normalize

NEIGHBOURHOOD_M = 3000
INTERSECTION_M = 300
CELL = 0.02  # Grid cell for nearest-neighbourhood lookups, in degrees.
POI_KEYS = (
    "aeroway", "amenity", "shop", "tourism", "leisure", "office", "healthcare", "craft",
    "building",
)  # fmt: skip
SKIP_POI = {
    "parking", "parking_entrance", "bicycle_parking", "bench", "waste_basket",
    "vending_machine", "loading_dock", "runway", "taxiway", "apron", "gate", "helipad",
}  # fmt: skip
PLACE_KINDS = {"city", "town", "village", "suburb", "neighbourhood", "quarter"}
STREET_TYPES = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
    "living_street", "pedestrian",
}  # fmt: skip


class Collector(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.addresses, self.pois, self.neighbourhoods = [], [], []
        self.node_street: dict[int, str] = {}
        self.crossings: dict[tuple[str, str], list] = {}

    def _entry(self, where, tags):
        number, street = tags.get("addr:housenumber"), tags.get("addr:street")
        if number and street:
            label = f"{number} {short_street(street)}"
            self.addresses.append((label, tags.get("addr:city"), where))
        name = tags.get("name")
        if not name or "public_transport" in tags or "railway" in tags or "highway" in tags:
            return
        for key in POI_KEYS:
            value = tags.get(key)
            if value and value not in SKIP_POI and (key != "aeroway" or value == "aerodrome"):
                kind = key if key == "building" else value.replace("_", " ")
                self.pois.append((name, kind, where))
                return

    def node(self, n):
        tags = n.tags
        if not tags:
            return
        where = (n.location.lon, n.location.lat)
        if tags.get("place") in PLACE_KINDS and tags.get("name"):
            self.neighbourhoods.append((tags["name"], where))
            return
        self._entry(where, tags)

    def way(self, w):
        tags = w.tags
        name = tags.get("name")
        if tags.get("highway") in STREET_TYPES and name:
            for nd in w.nodes:
                other = self.node_street.setdefault(nd.ref, name)
                if other != name:
                    try:
                        where = (nd.location.lon, nd.location.lat)
                    except osmium.InvalidLocationError:
                        continue
                    pair = tuple(sorted((short_street(other), short_street(name))))
                    spots = self.crossings.setdefault(pair, [])
                    if all(metres(where, s) > INTERSECTION_M for s in spots):
                        spots.append(where)
            return
        if not (tags.get("addr:housenumber") or tags.get("name")):
            return
        try:
            coords = [(nd.location.lon, nd.location.lat) for nd in w.nodes]
        except osmium.InvalidLocationError:
            return
        if coords:
            centre = (
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords),
            )
            self._entry(centre, tags)

    def area(self, a):
        """Places mapped as multipolygon relations (Pearson airport, some malls and
        parks); closed ways were already handled as ways."""
        if a.from_way() or not a.tags.get("name"):
            return
        coords = [(n.lon, n.lat) for ring in a.outer_rings() for n in ring]
        if coords:
            centre = (
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords),
            )
            self._entry(centre, {k: v for k, v in a.tags if k != "addr:housenumber"})


class Neighbourhoods:
    def __init__(self, places):
        self.cells: dict[tuple[int, int], list] = {}
        for name, where in places:
            self.cells.setdefault((int(where[0] // CELL), int(where[1] // CELL)), []).append(
                (name, where)
            )

    def nearest(self, where) -> str | None:
        cx, cy = int(where[0] // CELL), int(where[1] // CELL)
        best = (NEIGHBOURHOOD_M, None)
        for x in (cx - 1, cx, cx + 1):
            for y in (cy - 1, cy, cy + 1):
                for name, at in self.cells.get((x, y), ()):
                    m = metres(where, at)
                    if m < best[0]:
                        best = (m, name)
        return best[1]


def rows(c: Collector):
    """(text, label, detail, kind, lon, lat) for every entry."""
    areas = Neighbourhoods(c.neighbourhoods)
    seen = set()
    for label, city, where in c.addresses:
        detail = city or areas.nearest(where)
        key = (label, detail)
        if key in seen:
            continue  # Several buildings or entrances with the same address.
        seen.add(key)
        yield label, detail, "address", where
    for name, kind, where in c.pois:
        yield name, areas.nearest(where), kind, where
    for (a, b), spots in c.crossings.items():
        for where in spots:
            yield f"{a} & {b}", areas.nearest(where), "intersection", where


def build_index(source: str | os.PathLike, out_dir: str | os.PathLike) -> dict:
    """Write places.sqlite into `out_dir` from an OpenStreetMap file; returns counts."""
    c = Collector()
    c.apply_file(str(source), locations=True)
    out = Path(out_dir)
    handle, tmp = tempfile.mkstemp(dir=out, suffix=".sqlite.tmp")
    os.close(handle)
    counts: dict[str, int] = {}
    try:
        db = sqlite3.connect(tmp)
        db.execute(
            "CREATE VIRTUAL TABLE places USING fts5(name, area, label UNINDEXED,"
            " detail UNINDEXED, kind UNINDEXED, lon UNINDEXED, lat UNINDEXED)"
        )
        batch = []
        for label, detail, kind, (lon, lat) in rows(c):
            name, area = " ".join(normalize(label)), " ".join(normalize(detail or ""))
            batch.append((name, area, label, detail, kind, round(lon, 6), round(lat, 6)))
            group = kind if kind in ("address", "intersection") else "place"
            counts[group] = counts.get(group, 0) + 1
            if len(batch) >= 10_000:
                db.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
                batch.clear()
        db.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?)", batch)
        db.execute("INSERT INTO places(places) VALUES ('optimize')")
        db.commit()
        db.close()
        os.chmod(tmp, 0o644)
        os.replace(tmp, out / FILE_NAME)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"file": FILE_NAME, "bytes": (out / FILE_NAME).stat().st_size, "counts": counts}
