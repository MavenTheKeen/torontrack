"""TTC's official subway delay log (Toronto Open Data "TTC Subway Delay Data", updated
monthly), loaded so this project's observed gaps can be checked against incidents TTC
itself recorded. Station names in the log are free text: truncated at 22 characters,
suffixed with line codes ("KENNEDY BD STATION"), using old names ("DUNDAS") or describing
ranges ("UNION STATION TO KING"). match_station maps them to station keys and says how."""

import csv
import hashlib
import io
import json
import re
from datetime import UTC, datetime

from transit.bikeshare.parsing import FeedValidationError
from transit.db import connect
from transit.http import FetchError
from transit.ttc.realtime import TORONTO
from transit.ttc.sources import STATIC_HOST
from transit.ttc.static_gtfs import station_key
from transit.ttc.store import active_feed_version

PACKAGE = "ttc-subway-delay-data"
DELAYS_RESOURCE = "TTC Subway Delay Data since 2025.csv"
CODES_RESOURCE = "Code Descriptions.csv"

LINES = {"YU": "1", "YUS": "1", "BD": "2", "SHP": "4"}
BOUNDS = {"N": "Northbound", "S": "Southbound", "E": "Eastbound", "W": "Westbound"}
# Names in the log that differ from current GTFS station names.
ALIASES = {
    "vmc": "vaughan-metropolitan-centre",
    "yonge": "bloor-yonge",  # "YONGE BD STATION": the Line 2 side of Bloor-Yonge.
    "bloor": "bloor-yonge",
    "sheppard": "sheppard-yonge",
    "dundas": "tmu",  # Renamed in 2024.
    "eglinton-west": "cedarvale",  # Renamed in 2025.
    "north-york-ctr": "north-york-centre",
    "vaughan-mc": "vaughan-metropolitan-centre",
    "vaughan-metro-centre": "vaughan-metropolitan-centre",
    "main": "main-street",
}
# Words that qualify a station rather than name it.
QUALIFIERS = {"BD", "YU", "YUS", "SHP", "SUBWAY", "PLATFORM", "PLAT", "PL", "LINE", "TAIL"}
# Leading words that describe where relative to the station, not which station.
APPROACHING = re.compile(r"^(?:APPR?OACHING|APPRAOCHING|ENTERING|LEAVING)\s+")
TRUNCATED_AT = 22  # The log cuts the station field at this many characters.


def match_station(text: str, keys: set[str]) -> tuple[str | None, str]:
    """Return (station_key or None, how): exact, alias, prefix, range or unmatched."""
    name = (text or "").upper().strip()
    how = "exact"
    if " TO " in name:  # "UNION STATION TO KING": the first station of the section.
        name, how = name.split(" TO ", 1)[0], "range"
    name = APPROACHING.sub("", name)
    name = re.split(r"\s*[(-]\s*(?:PLAT|TOWARD|APPROACH|ENTER)", name)[0]
    name = re.split(r"\s+STAT(?:I(?:O(?:N)?)?)?\b", name)[0]
    words = [
        w
        for w in re.split(r"[\s.#]+", name)
        if w and w not in QUALIFIERS and not re.fullmatch(r"P?\d+", w)
    ]
    # A field cut mid-word may end in the start of "STATION".
    if len(text or "") >= TRUNCATED_AT and words and "STATION".startswith(words[-1]):
        words = words[:-1]
    key = station_key(" ".join(words))
    if not key:
        return None, "unmatched"
    if key in keys:
        return key, how
    if key in ALIASES:
        return ALIASES[key], "alias" if how == "exact" else how
    candidates = [k for k in keys if k.startswith(key)]
    if len(key) >= 5 and len(candidates) == 1:
        return candidates[0], "prefix" if how == "exact" else how
    return None, "unmatched"


def parse_delays(data: bytes, keys: set[str]) -> list[dict]:
    """Rows of the delay CSV with times in Toronto local time made explicit."""
    rows = []
    for r in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
        key, how = match_station(r["Station"], keys)
        delay_at = datetime.strptime(f"{r['Date']} {r['Time']}", "%Y-%m-%d %H:%M").replace(
            tzinfo=TORONTO
        )
        rows.append(
            {
                "source_id": int(r["_id"]),
                "delay_at": delay_at,
                "station_text": r["Station"],
                "station_key": key,
                "station_match": how,
                "line_text": r["Line"] or None,
                "route_id": LINES.get((r["Line"] or "").strip()),
                "bound": BOUNDS.get((r["Bound"] or "").strip()),
                "code": r["Code"] or None,
                "min_delay": int(r["Min Delay"] or 0),
                "min_gap": int(r["Min Gap"] or 0),
                "vehicle": r["Vehicle"] or None,
            }
        )
    return rows


def repair_text(text: str) -> str:
    """Undo double-encoded UTF-8 ("â€“" for an en dash), which the published code list
    contains. Text that does not round-trip cleanly is returned unchanged."""
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def parse_codes(data: bytes) -> dict[str, str]:
    return {
        r["CODE"].strip(): repair_text(r["DESCRIPTION"].strip())
        for r in csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
        if r.get("CODE")
    }


KEEP_FILES = 3  # Raw downloads kept for replay; each is a few MB.


def resource_urls(client) -> dict[str, str]:
    """Resolve the current download URLs from the open-data catalogue each time; the
    city has replaced resource URLs before."""
    body = client.fetch_bytes(
        f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}", host=STATIC_HOST
    )
    resources = {r["name"]: r["url"] for r in json.loads(body)["result"]["resources"]}
    missing = {DELAYS_RESOURCE, CODES_RESOURCE} - resources.keys()
    if missing:
        raise FeedValidationError(f"Delay data resources not found: {sorted(missing)}")
    return {"delays": resources[DELAYS_RESOURCE], "codes": resources[CODES_RESOURCE]}


def station_keys(conn) -> set[str]:
    version = active_feed_version(conn)
    return {
        r["station_key"]
        for r in conn.execute(
            "SELECT DISTINCT station_key FROM normalized.ttc_stops WHERE feed_version = %s",
            (version,),
        )
    }


def load(conn, file_sha256: str) -> dict:
    """(Re)build the normalized delay rows from one stored download."""
    raw = conn.execute(
        "SELECT * FROM raw.ttc_delay_files WHERE file_sha256 = %s", (file_sha256,)
    ).fetchone()
    keys = station_keys(conn)
    if not keys:
        raise FeedValidationError("Load TTC static GTFS (ttc-gtfs-refresh) first")
    rows = parse_delays(bytes(raw["delays_csv"]), keys)
    codes = parse_codes(bytes(raw["codes_csv"]))
    if not rows:
        raise FeedValidationError("The delay log has no rows")
    with conn.transaction():
        conn.execute("DELETE FROM normalized.ttc_official_delays")
        conn.execute("DELETE FROM normalized.ttc_delay_codes")
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO normalized.ttc_official_delays
                   (source_id, delay_at, min_delay, min_gap, station_text, station_key,
                    station_match, line_text, route_id, bound, code, vehicle, file_sha256)
                   VALUES (%(source_id)s, %(delay_at)s, %(min_delay)s, %(min_gap)s,
                           %(station_text)s, %(station_key)s, %(station_match)s,
                           %(line_text)s, %(route_id)s, %(bound)s, %(code)s, %(vehicle)s,
                           %(file)s)""",
                [{**r, "file": file_sha256} for r in rows],
            )
            cur.executemany(
                "INSERT INTO normalized.ttc_delay_codes VALUES (%s, %s, %s)",
                [(code, text, file_sha256) for code, text in codes.items()],
            )
    with_gap = [r for r in rows if r["min_gap"] > 0]
    return {
        "rows": len(rows),
        "rows_with_gap": len(with_gap),
        "matched_with_gap": sum(r["station_key"] is not None for r in with_gap),
        "latest_delay_at": max(r["delay_at"] for r in rows),
    }


def refresh(database_url: str, client, reload: bool = False) -> dict:
    """Download the log; store and load it only if it changed since the last download,
    or, with reload, re-normalize it anyway (after a matching or parsing fix)."""
    started = datetime.now(UTC)
    result = {"status": "failed", "file_sha256": None}
    with connect(database_url) as conn:
        try:
            urls = resource_urls(client)
            delays = client.fetch_bytes(urls["delays"], host=STATIC_HOST, max_bytes=100 << 20)
            codes = client.fetch_bytes(urls["codes"], host=STATIC_HOST, max_bytes=1 << 20)
            digest = hashlib.sha256(delays + b"\0" + codes).hexdigest()
            result["file_sha256"] = digest
            stored = conn.execute(
                """INSERT INTO raw.ttc_delay_files VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (file_sha256) DO NOTHING RETURNING file_sha256""",
                (digest, started, urls["delays"], urls["codes"], delays, codes),
            ).fetchone()
            current = conn.execute(
                "SELECT DISTINCT file_sha256 FROM normalized.ttc_official_delays"
            ).fetchall()
            if reload or stored or [r["file_sha256"] for r in current] != [digest]:
                result.update(load(conn, digest), status="loaded")
                conn.execute(
                    """DELETE FROM raw.ttc_delay_files WHERE file_sha256 NOT IN (
                           SELECT file_sha256 FROM raw.ttc_delay_files
                           ORDER BY fetched_at DESC LIMIT %s)""",
                    (KEEP_FILES,),
                )
            else:
                result["status"] = "unchanged"
        except Exception as exc:
            result["error"] = (
                str(exc)[:1000]
                if isinstance(exc, (FetchError, FeedValidationError))
                else type(exc).__name__
            )
        conn.execute(
            """INSERT INTO ops.ttc_delay_refreshes
               (started_at, finished_at, status, file_sha256, rows, rows_with_gap,
                matched_with_gap, latest_delay_at, error)
               VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["file_sha256"],
                result.get("rows"),
                result.get("rows_with_gap"),
                result.get("matched_with_gap"),
                result.get("latest_delay_at"),
                result.get("error"),
            ),
        )
    return result
