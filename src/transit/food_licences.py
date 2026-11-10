"""City of Toronto business licences of places that serve food, for "licensed here since".

The city publishes every business licence, current and cancelled, refreshed daily. A licence
belongs to its holder, so a new owner takes out a new one; a premises is shown as licensed
since the earliest licence at its address under a matching name, which carries through a
change of owner that keeps the name. Only the category, operating name, street address and
dates are kept: no holder names or phone numbers.

Contains information licensed under the Open Government Licence – Toronto.
"""

import csv
import hashlib
import io
import json
import re
import tempfile
from collections import defaultdict
from datetime import UTC, date, datetime

from transit.db import connect
from transit.food_inspections import CURRENT, _blank, _tokens
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "municipal-licensing-and-standards-business-licences-and-permits"
MAX_BYTES = 200 << 20
CATEGORIES = {
    "EATING OR DRINKING ESTABLISHMENT",
    "EXPANDED EATING/DRINKING ESTABLISHMENT",
    "TAKE-OUT OR RETAIL FOOD ESTABLISHMENT",
}
# The file has had over 60,000 food licences; far fewer means a truncated or broken export.
MIN_LICENCES = 20000
POSTAL = re.compile(r"\s+[A-Z]\d[A-Z]\s?\d[A-Z]\d$")


def address_key(address: str) -> str:
    """'2300 YONGE ST, S106' and '2300 Yonge St M4P 1E4' -> '2300 YONGE ST': the street
    address without unit, postal code or punctuation."""
    text = POSTAL.sub("", address.upper().strip()).split(",")[0]
    return " ".join(re.sub(r"[^A-Z0-9 ]", "", text).split())


def read(feed) -> tuple[dict, int]:
    """Food licences by number from the city's CSV export (a text stream). Rows without a
    number, address or issue date are skipped and counted."""
    licences = {}
    skipped = 0
    for row in csv.DictReader(feed):
        if row.get("Category") not in CATEGORIES:
            continue
        try:
            number = _blank(row["Licence No."])
            key = address_key(row["Licence Address Line 1"] or "")
            issued = date.fromisoformat(row["Issued"][:10])
            cancelled = _blank(row["Cancel Date"])
            cancelled = date.fromisoformat(cancelled[:10]) if cancelled else None
        except (KeyError, ValueError):
            skipped += 1
            continue
        if not number or not key:
            skipped += 1
            continue
        licences[number] = (
            number,
            row["Category"],
            _blank(row["Operating Name"]),
            key,
            issued,
            cancelled,
        )
    return licences, skipped


def load(conn, licences: dict, now: datetime) -> None:
    """Upsert every licence in one transaction; ones that leave the file are kept."""
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE licences_in (LIKE normalized.food_licences) ON COMMIT DROP")
        with cur.copy(
            """COPY licences_in (licence_no, category, operating_name, address_key, issued_on,
                   cancelled_on, last_seen_at) FROM STDIN"""
        ) as copy:
            for row in licences.values():
                copy.write_row((*row, now))
        cur.execute(
            """INSERT INTO normalized.food_licences SELECT * FROM licences_in
               ON CONFLICT (licence_no) DO UPDATE SET category = EXCLUDED.category,
                 operating_name = EXCLUDED.operating_name, address_key = EXCLUDED.address_key,
                 issued_on = EXCLUDED.issued_on, cancelled_on = EXCLUDED.cancelled_on,
                 last_seen_at = EXCLUDED.last_seen_at"""
        )


def _resource_url(meta: dict) -> str:
    for resource in meta["result"]["resources"]:
        if resource.get("datastore_active"):
            return f"https://{STATIC_HOST}/datastore/dump/{resource['id']}"
    raise FetchError("Business licences datastore resource not found")


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
                    """SELECT feed_sha256 FROM ops.food_licence_refreshes
                       WHERE status = 'loaded' ORDER BY refresh_id DESC LIMIT 1"""
                ).fetchone()
                if last and last["feed_sha256"] == digest:
                    result["status"] = "unchanged"
                else:
                    feed.seek(0)
                    text = io.TextIOWrapper(
                        feed, encoding="utf-8-sig", errors="replace", newline=""
                    )
                    licences, skipped = read(text)
                    text.detach()  # Leave the file to the with block.
                    if len(licences) < MIN_LICENCES:
                        raise FetchError(
                            f"Business licences export lists only {len(licences)} food "
                            "licences; not loaded"
                        )
                    load(conn, licences, started)
                    result.update(status="loaded", licences=len(licences), skipped=skipped)
        except (FetchError, KeyError, ValueError, csv.Error) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.food_licence_refreshes (started_at, finished_at, status,
                   feed_sha256, licences, skipped, error)
               VALUES (%s, now(), %s, %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["feed_sha256"],
                result.get("licences"),
                result.get("skipped"),
                result.get("error"),
            ),
        )
    return result


def since(conn, premises: list[dict]) -> dict[str, date]:
    """Premises ID -> the earliest issue date of a food licence at the premises' address
    whose operating name shares at least half the words of the shorter name."""
    keys = {address_key(p["address"]) for p in premises}
    by_key = defaultdict(list)
    for r in conn.execute(
        """SELECT address_key, operating_name, issued_on FROM normalized.food_licences
           WHERE address_key = ANY(%s) AND operating_name IS NOT NULL""",
        (list(keys),),
    ):
        by_key[r["address_key"]].append((_tokens(r["operating_name"]), r["issued_on"]))
    found = {}
    for p in premises:
        words = _tokens(p["name"])
        dates = [
            issued
            for licence_words, issued in by_key.get(address_key(p["address"]), ())
            if words
            and licence_words
            and len(words & licence_words) / min(len(words), len(licence_words)) >= 0.5
        ]
        if dates:
            found[p["premises_id"]] = min(dates)
    return found


def since_listed(conn) -> dict[str, date]:
    """since() for every premises in the city's current list."""
    rows = conn.execute(
        f"SELECT p.premises_id, p.name, p.address FROM normalized.food_premises p WHERE {CURRENT}"
    ).fetchall()
    return since(conn, rows)
