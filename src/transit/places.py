"""A search index of addresses, named places and intersections, for the trip planner.

Built by transit/place_index.py from the same OpenStreetMap extract as the map layers into
places.sqlite next to the street map, so a trip can start or end at a typed address without
the visitor sharing their location and without a third-party geocoding service:

- addresses: anything with addr:housenumber and addr:street ("100 Queen St W");
- named places: shops, restaurants, schools, parks, offices, hospitals, attractions and
  named buildings ("Royal Ontario Museum"). Transit stops and stations are left out; the
  map already searches them from the schedules;
- intersections: two differently named streets that share a node ("Queen St W & Spadina
  Ave"), one entry per pair within 300 m of each other.

Each entry's detail is its city (addr:city) or else the nearest neighbourhood, suburb or
town node within 3 km, so that "10 Main St" in two places can be told apart.
The text is indexed with SQLite FTS5 in a normalized form in which street words are
abbreviated ("Street" -> "st", "West" -> "w"); queries are normalized the same way.

© OpenStreetMap contributors, available under the Open Database Licence.
"""

import math
import os
import re
import sqlite3
from pathlib import Path

FILE_NAME = "places.sqlite"
# Places people travel to by name, listed before shops and offices that share a word.
MAJOR = {
    "aerodrome", "terminal", "mall", "attraction", "museum", "gallery", "zoo", "theme park",
    "stadium", "university", "college", "hospital", "library", "theatre", "arts centre",
    "community centre", "sports centre", "townhall", "convention centre",
}  # fmt: skip
# Full words and their short forms, applied to both the index and queries.
WORDS = {
    "street": "st", "avenue": "ave", "road": "rd", "boulevard": "blvd", "drive": "dr",
    "crescent": "cres", "court": "crt", "place": "pl", "square": "sq", "lane": "ln",
    "terrace": "terr", "parkway": "pkwy", "highway": "hwy", "circle": "cir", "gardens": "gdns",
    "east": "e", "west": "w", "north": "n", "south": "s", "saint": "st",
}  # fmt: skip
# Words people put between two streets: "Queen and Spadina", "Queen & Spadina", "Queen at".
JOINERS = {"and", "at"}
M_PER_DEG_LAT = 111_195.0


def normalize(text: str) -> list[str]:
    """'100 Queen Street West' -> ['100', 'queen', 'st', 'w']."""
    tokens = re.findall(r"[0-9]+[a-z]?|[^\W\d_]+", text.lower())
    return [WORDS.get(t, t) for t in tokens]


def query_tokens(text: str) -> list[str]:
    return [t for t in normalize(text) if t not in JOINERS]


def metres(a, b) -> float:
    dx = (b[0] - a[0]) * math.cos(math.radians(a[1]))
    return math.hypot(dx, b[1] - a[1]) * M_PER_DEG_LAT


def _rank(kind: str, intent: str) -> int:
    """How well an entry's kind fits what the query looks like: an address ("100 Queen"),
    an intersection ("Queen and Spadina") or a name ("Royal Ontario Museum")."""
    if intent == "address":
        return 0 if kind == "address" else 1
    if intent == "intersection":
        return 0 if kind == "intersection" else 1
    return 0 if kind in MAJOR else 1 if kind not in ("address", "intersection") else 2


def search(path: str | os.PathLike, text: str, near=None, limit: int = 8) -> list[dict]:
    """Entries matching every word of `text` as a prefix, in their name or neighbourhood,
    best first: those matching by name alone, then the kinds that fit the query, then those
    with every finished word whole ("queen" is not "Queens"; the last word may still be
    being typed), then those whose name starts with it, then the nearest to `near`
    (lon, lat), then the shortest."""
    tokens = query_tokens(text)
    words = normalize(text)
    intent = (
        "address"
        if tokens and tokens[0][0].isdigit()
        else "intersection"
        if len(words) > len(tokens) or re.search(r"[&/@]", text)
        else "name"
    )
    if not tokens or not Path(path).is_file():
        return []
    match = " ".join(f'"{t}"*' for t in tokens)
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        columns = "SELECT name, label, detail, kind, lon, lat FROM places WHERE places MATCH ?"
        found = db.execute(f"{columns} ORDER BY bm25(places, 10.0, 1.0) LIMIT 400", (match,))
        found = set(found.fetchall())
        # Major places are fetched on their own, so that a common word ("Pearson", also a
        # street with hundreds of addresses) cannot push the airport out of the candidates.
        major = ",".join("?" * len(MAJOR))
        found |= set(db.execute(f"{columns} AND kind IN ({major}) LIMIT 50", (match, *MAJOR)))
    finally:
        db.close()
    wanted = " ".join(tokens)

    def score(row):
        name = row[0].split()
        by_name = all(any(w.startswith(t) for w in name) for t in tokens)
        far = metres(near, (row[4], row[5])) / 1000 if near else 0
        whole = all(t in name for t in tokens[:-1])
        starts = row[0].startswith(wanted)
        return (not by_name, _rank(row[3], intent), not whole, not starts, round(far), len(row[1]))

    out = []
    for _, label, detail, kind, lon, lat in sorted(found, key=score):
        # The same name twice close together (an address with and without its city) is one.
        if any(o["name"] == label and metres((o["lon"], o["lat"]), (lon, lat)) < 300 for o in out):
            continue
        out.append({"name": label, "detail": detail, "kind": kind, "lon": lon, "lat": lat})
        if len(out) == limit:
            break
    return out
