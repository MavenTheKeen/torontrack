"""Notable places to eat in Toronto from Wikidata, for the food map.

A place counts as notable when Wikidata links it to an English Wikipedia article or records
a Michelin Guide award. Places Wikidata marks as dissolved or closed are left out. Each load
replaces the list, so places removed from Wikidata leave the map. The map shows a notable
place only where it matches an OpenStreetMap place to eat (match), which also leaves out
places that have closed without Wikidata recording it.

Wikidata is published under CC0.
"""

import json
import math
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from transit.db import connect
from transit.food_inspections import BOUNDS, _tokens
from transit.http import FeedClient, FetchError

HOST = "query.wikidata.org"
# Wikidata asks automated clients to name themselves with a way to reach the operator.
USER_AGENT = "Torontrack/0.1 (https://torontrack.skynetlab.duckdns.org)"
# Restaurant, café, bar, pub, bakery, coffeehouse, diner, fast food restaurant, gay bar,
# brewpub, tavern, delicatessen.
TYPES = (
    "Q11707 Q30022 Q187456 Q212198 Q274393 Q1501212 Q178706 Q1043639 Q36521 Q2100735 "
    "Q5307737 Q11666766 Q1323756"
)
SOUTH, NORTH, WEST, EAST = BOUNDS
QUERY = f"""
SELECT ?place ?name ?coord (MIN(?inception) AS ?opened) (SAMPLE(?article) AS ?wikipedia)
       (GROUP_CONCAT(DISTINCT ?award; separator="; ") AS ?awards) WHERE {{
  SERVICE wikibase:box {{
    ?place wdt:P625 ?coord .
    bd:serviceParam wikibase:cornerSouthWest "Point({WEST} {SOUTH})"^^geo:wktLiteral .
    bd:serviceParam wikibase:cornerNorthEast "Point({EAST} {NORTH})"^^geo:wktLiteral .
  }}
  ?place wdt:P31 ?type . VALUES ?type {{ {" ".join("wd:" + q for q in TYPES.split())} }}
  ?place rdfs:label ?name . FILTER(LANG(?name) = "en")
  FILTER NOT EXISTS {{ ?place wdt:P576 [] }}
  FILTER NOT EXISTS {{ ?place wdt:P3999 [] }}
  OPTIONAL {{ ?place wdt:P571 ?inception }}
  OPTIONAL {{ ?article schema:about ?place ; schema:isPartOf <https://en.wikipedia.org/> }}
  OPTIONAL {{ ?place wdt:P166 ?prize . ?prize rdfs:label ?award .
             FILTER(LANG(?award) = "en" && CONTAINS(?award, "Michelin")) }}
}}
GROUP BY ?place ?name ?coord
"""
URL = f"https://{HOST}/sparql?" + urlencode({"query": QUERY, "format": "json"})
# Wikidata has listed about 60 notable places in the box; far fewer means a broken answer.
MIN_PLACES = 20
MATCH_M = 150  # Wikidata coordinates are often a building or block, not the door.
POINT = re.compile(r"Point\(([-\d.]+) ([-\d.]+)\)")


def read(payload: dict) -> list[tuple]:
    """(Wikidata ID, name, lat, lon, opening year, Wikipedia URL, Michelin awards) for each
    notable place in a SPARQL JSON answer."""
    found = {}
    for row in payload["results"]["bindings"]:
        qid = row["place"]["value"].rsplit("/", 1)[-1]
        point = POINT.fullmatch(row["coord"]["value"])
        wikipedia = row.get("wikipedia", {}).get("value")
        awards = row.get("awards", {}).get("value") or None
        if not point or not (wikipedia or awards) or qid in found:
            continue
        opened = row.get("opened", {}).get("value", "")
        year = int(opened[:4]) if re.match(r"\d{4}-", opened) else None
        lon, lat = float(point[1]), float(point[2])
        found[qid] = (qid, row["name"]["value"], lat, lon, year, wikipedia, awards)
    return list(found.values())


def load(conn, places: list[tuple], now: datetime) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM normalized.notable_eats")
        with cur.copy(
            """COPY normalized.notable_eats (wikidata_id, name, lat, lon, opened, wikipedia,
                   awards, loaded_at) FROM STDIN"""
        ) as copy:
            for row in places:
                copy.write_row((*row, now))


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed"}
    with connect(database_url) as conn:
        try:
            payload = json.loads(
                client.fetch_bytes(
                    URL,
                    host=HOST,
                    headers={"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT},
                )
            )
            places = read(payload)
            if len(places) < MIN_PLACES:
                raise FetchError(f"Wikidata lists only {len(places)} notable places; not loaded")
            load(conn, places, started)
            result.update(status="loaded", places=len(places))
        except (FetchError, KeyError, ValueError, TypeError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.notable_eat_refreshes (started_at, finished_at, status, places,
                   error) VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], result.get("places"), result.get("error")),
        )
    return result


def listed(conn) -> list[dict]:
    return conn.execute(
        """SELECT wikidata_id, name, lat, lon, opened, wikipedia, awards
           FROM normalized.notable_eats ORDER BY wikidata_id"""
    ).fetchall()


def match(places: list[list], notable: list[dict]) -> dict[int, dict]:
    """Index in `places` ([lon, lat, name, ...]) -> the notable place it is: the nearest
    place within MATCH_M sharing at least half the words of the shorter name."""
    found = {}
    for n in notable:
        words = _tokens(n["name"])
        if not words:
            continue
        best = None
        scale = math.cos(math.radians(n["lat"]))
        for index, (lon, lat, name, *_) in enumerate(places):
            if abs(lat - n["lat"]) > 0.002 or abs(lon - n["lon"]) > 0.003:
                continue
            metres = 111_195 * math.hypot((lon - n["lon"]) * scale, lat - n["lat"])
            place_words = _tokens(name)
            if (
                metres <= MATCH_M
                and place_words
                and len(words & place_words) / min(len(words), len(place_words)) >= 0.5
                and (best is None or metres < best[0])
            ):
                best = (metres, index)
        if best:
            found[best[1]] = n
    return found
