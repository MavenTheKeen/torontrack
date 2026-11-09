"""City of Toronto food premises inspections (DineSafe), for the map and place search.

The city publishes every inspection of the last two to three years as one table, refreshed
daily: one row per infraction found, or one row for an inspection with none. Each premises
keeps its city ID; an inspection is identified by premises and date. Inspections are kept
after they leave the city's file, so the history grows past its window.

Contains information licensed under the Open Government Licence – Toronto.
"""

import csv
import hashlib
import io
import json
import math
import re
import tempfile
import unicodedata
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "dinesafe"
MAX_BYTES = 200 << 20
BOUNDS = (43.5, 43.9, -79.7, -79.05)
# The file has had about 20,000 premises; far fewer means a truncated or broken export.
MIN_PREMISES = 5000
SEVERITIES = {
    "M - Minor": "minor",
    "S - Significant": "significant",
    "C - Crucial": "crucial",
    "NA": "not applicable",
}
OUTCOMES = {"Conviction: Fined": "Conviction - Fined"}


def _blank(value: str | None) -> str | None:
    value = (value or "").strip()
    return None if value in ("", "None") else value


def _address(value: str) -> str:
    # Empty address parts are exported as the word None, e.g. "1871 O'Connor Dr None M4A 1X1".
    return " ".join(part for part in value.split() if part != "None")


def _fine(value: str | None) -> Decimal | None:
    value = _blank(value)
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Fine {value!r} is not a number") from exc


def read(feed) -> tuple[dict, dict, list, int]:
    """Premises, inspections and infractions from the city's CSV export (a text stream).
    Rows outside the city or without a premises, date or status are skipped and counted."""
    premises, inspections, infractions = {}, {}, []
    skipped = 0
    south, north, west, east = BOUNDS
    for row in csv.DictReader(feed):
        try:
            pid = _blank(row["estId"])
            inspected = date.fromisoformat(row["inspectionDate"])
            status = _blank(row["inspectionStatus"])
            lat, lon = float(row["latitude"]), float(row["longitude"])
        except (KeyError, ValueError):
            skipped += 1
            continue
        if not pid or not status or not (south <= lat <= north and west <= lon <= east):
            skipped += 1
            continue
        premises[pid] = {
            "premises_id": pid,
            "name": row["estName"].strip(),
            "address": _address(row["address"]),
            "lat": lat,
            "lon": lon,
        }
        inspections.setdefault((pid, inspected), status)
        infraction = _blank(row["typeDesc"])
        if infraction:
            outcome = _blank(row["OutcomeDesc"])
            outcome_on = _blank(row["OutcomeDate"])
            infractions.append(
                (
                    row["unique_id"],
                    pid,
                    inspected,
                    infraction,
                    _blank(row["deficiencyDesc"]),
                    SEVERITIES.get(row["severity"]),
                    OUTCOMES.get(outcome, outcome),
                    date.fromisoformat(outcome_on[:10]) if outcome_on else None,
                    _fine(row["amountFined"]),
                )
            )
    return premises, inspections, infractions, skipped


def load(conn, premises: dict, inspections: dict, infractions: list, now: datetime) -> None:
    """Upsert premises and inspections, and replace the infractions of every inspection in
    the file, in one transaction."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """CREATE TEMP TABLE premises_in (LIKE normalized.food_premises) ON COMMIT DROP"""
        )
        with cur.copy(
            "COPY premises_in (premises_id, name, address, lat, lon, last_seen_at) FROM STDIN"
        ) as copy:
            for p in premises.values():
                copy.write_row((p["premises_id"], p["name"], p["address"], p["lat"], p["lon"], now))
        cur.execute(
            """INSERT INTO normalized.food_premises SELECT * FROM premises_in
               ON CONFLICT (premises_id) DO UPDATE SET name = EXCLUDED.name,
                 address = EXCLUDED.address, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                 last_seen_at = EXCLUDED.last_seen_at"""
        )
        cur.execute(
            """CREATE TEMP TABLE inspections_in (LIKE normalized.food_inspections)
               ON COMMIT DROP"""
        )
        with cur.copy("COPY inspections_in (premises_id, inspected_on, status) FROM STDIN") as copy:
            for (pid, inspected), status in inspections.items():
                copy.write_row((pid, inspected, status))
        cur.execute(
            """INSERT INTO normalized.food_inspections SELECT * FROM inspections_in
               ON CONFLICT (premises_id, inspected_on) DO UPDATE SET status = EXCLUDED.status"""
        )
        cur.execute(
            """DELETE FROM normalized.food_infractions f USING inspections_in i
               WHERE f.premises_id = i.premises_id AND f.inspected_on = i.inspected_on"""
        )
        with cur.copy(
            """COPY normalized.food_infractions (infraction_id, premises_id, inspected_on,
                   infraction, category, severity, outcome, outcome_on, fine) FROM STDIN"""
        ) as copy:
            for row in infractions:
                copy.write_row(row)


def _resource_url(meta: dict) -> str:
    """The table export of the datastore copy, which carries the newest inspections."""
    for resource in meta["result"]["resources"]:
        if resource.get("datastore_active"):
            return f"https://{STATIC_HOST}/datastore/dump/{resource['id']}"
    raise FetchError("DineSafe datastore resource not found")


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed", "feed_sha256": None}
    with connect(database_url) as conn:
        try:
            meta = json.loads(
                client.fetch_bytes(
                    f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}",
                    host=STATIC_HOST,
                )
            )
            with tempfile.TemporaryFile() as feed:
                client.fetch_bytes(
                    _resource_url(meta), host=STATIC_HOST, max_bytes=MAX_BYTES, sink=feed
                )
                feed.seek(0)
                digest = hashlib.file_digest(feed, "sha256").hexdigest()
                result["feed_sha256"] = digest
                last = conn.execute(
                    """SELECT feed_sha256 FROM ops.food_inspection_refreshes
                       WHERE status = 'loaded' ORDER BY refresh_id DESC LIMIT 1"""
                ).fetchone()
                if last and last["feed_sha256"] == digest:
                    result["status"] = "unchanged"
                else:
                    feed.seek(0)
                    text = io.TextIOWrapper(feed, encoding="utf-8-sig", newline="")
                    premises, inspections, infractions, skipped = read(text)
                    text.detach()  # Leave the file to the with block.
                    if len(premises) < MIN_PREMISES:
                        raise FetchError(
                            f"DineSafe export lists only {len(premises)} premises; not loaded"
                        )
                    load(conn, premises, inspections, infractions, started)
                    result.update(
                        status="loaded",
                        premises=len(premises),
                        inspections=len(inspections),
                        infractions=len(infractions),
                        skipped=skipped,
                    )
        except (FetchError, KeyError, ValueError, csv.Error) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.food_inspection_refreshes (started_at, finished_at, status,
                   feed_sha256, premises, inspections, infractions, skipped, error)
               VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["feed_sha256"],
                result.get("premises"),
                result.get("inspections"),
                result.get("infractions"),
                result.get("skipped"),
                result.get("error"),
            ),
        )
    return result


# Reading, for the map and place search. Only premises in the newest load are shown: one
# that has left the city's file has closed or changed hands.
STATUS_CODES = {"Pass": 0, "Conditional Pass": 1, "Closed": 2}
SEARCH_LIMIT = 4
CLEAN_DAYS = 730
HISTORY = 10
SEVERITY_ORDER = ["crucial", "significant", "minor", "not applicable"]
CURRENT = "p.last_seen_at = (SELECT max(last_seen_at) FROM normalized.food_premises)"
LATEST = """JOIN LATERAL (SELECT i.status, i.inspected_on FROM normalized.food_inspections i
                 WHERE i.premises_id = p.premises_id
                 ORDER BY i.inspected_on DESC LIMIT 1) i ON true"""


# Most names and many addresses are published in capitals; they are shown in normal case.
# Street abbreviations have no vowels but are not acronyms.
STREET_WORDS = {
    "st", "rd", "dr", "blvd", "crt", "pl", "pkwy", "ln", "hwy", "sq", "gdns", "pk", "trl", "grv",
}  # fmt: skip
SMALL_WORDS = {"and", "of", "the", "de", "la", "le", "du", "des", "on", "at", "by", "for", "in"}
POSTAL = re.compile(r"[A-Z]\d[A-Z]|\d[A-Z]\d")
# Acronyms with vowels, which the no-vowel rule below would miss.
ACRONYMS = {"YMCA", "YWCA", "LCBO", "IGA", "CIBC", "CNE", "UHN", "AGO", "ROM", "OCAD", "USA"}
ROMAN = {"II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII"}


def _word(word: str, first: bool) -> str:
    if (
        POSTAL.fullmatch(word)
        or word in ROMAN
        or word.strip(".,") in ACRONYMS
        or word in ("N", "S", "E", "W")
    ):
        return word
    lower = word.lower()
    letters = re.sub(r"[^a-z]", "", lower)
    if not letters or word[0].isdigit():
        return lower  # 2ND -> 2nd, #3, 1/2
    if not re.search("[aeiouy]", letters) and len(letters) <= 4 and letters not in STREET_WORDS:
        return word  # BBQ, KFC, TD
    if lower in SMALL_WORDS and not first:
        return lower
    out = "-".join(part[:1].upper() + part[1:] for part in lower.split("-"))
    if out.startswith("Mc") and len(out) > 3:
        out = "Mc" + out[2].upper() + out[3:]
    # O'CONNOR -> O'Connor, but MCDONALD'S -> McDonald's.
    return re.sub(r"^(\w)'(\w)", lambda m: f"{m[1]}'{m[2].upper()}", out)


def display(text: str) -> str:
    """'PIZZA NOVA' -> 'Pizza Nova', '1871 O'CONNOR DR M4A 1X1' -> '1871 O'Connor Dr M4A 1X1';
    text already in mixed case is kept as published."""
    if any(c.islower() for c in text):
        return text
    words = text.split()
    # A small word after a number starts a street name: 222 The Esplanade.
    return " ".join(_word(w, i == 0 or words[i - 1][0].isdigit()) for i, w in enumerate(words))


def _shown(row) -> dict:
    return {**row, "name": display(row["name"]), "address": display(row["address"])}


def _listed(conn, today: date) -> list[dict]:
    """Every listed premises with its latest result code (STATUS_CODES, or 3 for any other
    status) and clean record: a latest Pass and no significant or crucial infraction in
    the CLEAN_DAYS before `today`."""
    rows = conn.execute(
        f"""SELECT p.premises_id, p.name, p.lat, p.lon, i.status,
                   EXISTS (SELECT 1 FROM normalized.food_infractions f
                           WHERE f.premises_id = p.premises_id
                             AND f.inspected_on >= %s
                             AND f.severity IN ('significant', 'crucial')) AS serious
            FROM normalized.food_premises p {LATEST} WHERE {CURRENT}""",
        (today - timedelta(days=CLEAN_DAYS),),
    )
    return [
        {
            **r,
            "code": STATUS_CODES.get(r["status"], 3),
            "clean": int(r["status"] == "Pass" and not r["serious"]),
        }
        for r in rows
    ]


def _point(r) -> list:
    return [r["premises_id"], round(r["lat"], 5), round(r["lon"], 5), r["code"], r["clean"]]


def points(conn, today: date) -> list[list]:
    """[premises ID, lat, lon, latest result code, clean record] for every listed premises."""
    return [_point(r) for r in _listed(conn, today)]


# The food map shows places to eat from OpenStreetMap (transit.places.eating_places), each
# matched to the inspected premises of the same name within MATCH_M. The city's list has no
# type of premises, so grocers, institutional kitchens and the like are kept apart.
EAT_KINDS = {"restaurant": 0, "cafe": 1, "fast food": 2, "bar": 3, "pub": 3, "bakery": 4,
             "ice cream": 4}  # fmt: skip
MATCH_M = 80
NAME_NOISE = {"the", "and", "restaurant", "cafe", "inc", "ltd", "bar", "grill", "kitchen", "co"}
CELL = 0.002  # degrees, about 220 m north to south; MATCH_M fits within one cell around


def _tokens(name: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z0-9]+", plain.replace("'", ""))) - NAME_NOISE


def _cell(lat: float, lon: float) -> tuple[int, int]:
    return round(lat / CELL), round(lon / CELL)


def eating_places(conn, osm: list[tuple[str, str, float, float]], today: date) -> dict:
    """Places to eat, [lon, lat, name, kind code, premises ID, result code, clean record]
    (the last three null without a matched premises), and the listed premises matched to
    none, as in points(). A premises matches the nearest place within MATCH_M sharing at
    least half the words of the shorter name; each place takes its nearest match."""
    grid = defaultdict(list)
    for index, (name, kind, lon, lat) in enumerate(osm):
        if kind in EAT_KINDS:
            grid[_cell(lat, lon)].append((index, lon, lat, _tokens(name)))
    claimed: dict[int, tuple[float, dict]] = {}
    listed = _listed(conn, today)
    for r in listed:
        words = _tokens(r["name"])
        if not words:
            continue
        y, x = _cell(r["lat"], r["lon"])
        best = None
        for cell in ((y + dy, x + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)):
            for index, lon, lat, place_words in grid.get(cell, ()):
                if not place_words:
                    continue
                shared = len(words & place_words) / min(len(words), len(place_words))
                metres = 111_195 * math.hypot(
                    (lon - r["lon"]) * math.cos(math.radians(lat)), lat - r["lat"]
                )
                if shared >= 0.5 and metres <= MATCH_M and (best is None or metres < best[0]):
                    best = (metres, index)
        if best and (best[1] not in claimed or best[0] < claimed[best[1]][0]):
            claimed[best[1]] = (best[0], r)
    places = []
    for index, (name, kind, lon, lat) in enumerate(osm):
        if kind not in EAT_KINDS:
            continue
        r = claimed.get(index, (None, None))[1]
        match = [r["premises_id"], r["code"], r["clean"]] if r else [None, None, None]
        places.append([round(lon, 5), round(lat, 5), name, EAT_KINDS[kind], *match])
    matched = {r["premises_id"] for _, r in claimed.values()}
    other = [_point(r) for r in listed if r["premises_id"] not in matched]
    return {"places": places, "other": other}


def _like(word: str) -> str:
    return "%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search(conn, text: str, near: tuple[float, float] | None = None) -> list[dict]:
    """Listed premises whose name contains every word of `text`, names starting with it
    first, then nearest to `near` (lat, lon)."""
    words = text.lower().split()[:5]
    if not words:
        return []
    lat, lon = near or (43.6532, -79.3832)
    where = " AND ".join("lower(p.name) LIKE %s" for _ in words)
    return [
        _shown(r)
        for r in conn.execute(
            f"""SELECT p.premises_id, p.name, p.address, p.lat, p.lon, i.status, i.inspected_on
                FROM normalized.food_premises p {LATEST}
                WHERE {CURRENT} AND {where}
                ORDER BY lower(p.name) LIKE %s DESC,
                         (p.lat - %s) ^ 2 + ((p.lon - %s) * cos(radians(%s))) ^ 2
                LIMIT {SEARCH_LIMIT}""",
            [*map(_like, words), _like(text.lower().strip())[1:], lat, lon, lat],
        )
    ]


def premises(conn, premises_id: str) -> dict | None:
    """One premises with its latest HISTORY inspections, newest first, each with its
    infractions, most severe first."""
    place = conn.execute(
        f"""SELECT p.premises_id, p.name, p.address, p.lat, p.lon, {CURRENT} AS listed,
                   (SELECT count(*) FROM normalized.food_inspections i
                    WHERE i.premises_id = p.premises_id) AS inspections_total
            FROM normalized.food_premises p WHERE p.premises_id = %s""",
        (premises_id,),
    ).fetchone()
    if place is None:
        return None
    inspections = [
        {**r, "infractions": []}
        for r in conn.execute(
            """SELECT inspected_on, status FROM normalized.food_inspections
               WHERE premises_id = %s ORDER BY inspected_on DESC LIMIT %s""",
            (premises_id, HISTORY),
        )
    ]
    by_date = {i["inspected_on"]: i for i in inspections}
    if inspections:
        for r in conn.execute(
            """SELECT inspected_on, infraction, category, severity, outcome, outcome_on, fine
               FROM normalized.food_infractions
               WHERE premises_id = %s AND inspected_on >= %s
               ORDER BY array_position(%s, severity), infraction""",
            (premises_id, inspections[-1]["inspected_on"], SEVERITY_ORDER),
        ):
            day = r.pop("inspected_on")
            if day in by_date:
                by_date[day]["infractions"].append(r)
    return {**_shown(place), "inspections": inspections}
