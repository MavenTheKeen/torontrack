"""Toronto beach water quality (E. coli samples), for the city map and the findings.

Toronto Public Health samples the city's supervised beaches in the swimming season, at
several sites per beach, and publishes every result since 2007. A beach is shown as
unsafe for swimming on a day when the geometric mean of its sites' E. coli counts is
above UNSAFE per 100 mL, Toronto's standard (Ontario and Canada use 200). Each load
replaces the samples.

Contains information licensed under the Open Government Licence – Toronto.
"""

import json
from datetime import UTC, date, datetime

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "toronto-beaches-water-quality"
MAX_BYTES = 80 << 20
UNSAFE = 100  # E. coli per 100 mL.
MIN_SAMPLES = 10_000
RECENT = 7  # Sampling days shown per beach.


def read(rows: list[dict]) -> list[tuple]:
    """(beach ID, beach name, site, day, E. coli per 100 mL, lon, lat) for each sample."""
    samples = []
    for row in rows:
        try:
            geometry = json.loads(row["geometry"])
            lon, lat = geometry["coordinates"][:2]
            ecoli = int(float(row["eColi"]))
            day = date.fromisoformat(str(row["collectionDate"])[:10])
            beach = int(row["beachId"])
        except (KeyError, TypeError, ValueError):
            continue
        samples.append((beach, row["beachName"].strip(), str(row["siteName"]).strip(), day,
                        ecoli, float(lon), float(lat)))  # fmt: skip
    return samples


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed"}
    with connect(database_url) as conn:
        try:
            meta = json.loads(
                client.fetch_bytes(
                    f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}",
                    host=STATIC_HOST,
                )
            )
            [resource] = [r for r in meta["result"]["resources"] if r.get("datastore_active")][:1]
            body = json.loads(
                client.fetch_bytes(
                    f"https://{STATIC_HOST}/datastore/dump/{resource['id']}?format=json",
                    host=STATIC_HOST,
                    max_bytes=MAX_BYTES,
                )
            )
            fields = [f["id"] for f in body["fields"]]
            samples = read([dict(zip(fields, r, strict=True)) for r in body["records"]])
            if len(samples) < MIN_SAMPLES:
                raise FetchError(f"Only {len(samples)} beach samples; not loaded")
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM normalized.beach_samples")
                with cur.copy(
                    """COPY normalized.beach_samples (beach_id, beach_name, site, day, ecoli,
                           lon, lat) FROM STDIN"""
                ) as copy:
                    for row in samples:
                        copy.write_row(row)
            result.update(status="loaded", samples=len(samples))
        except (FetchError, KeyError, ValueError, TypeError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.beach_refreshes (started_at, finished_at, status, samples, error)
               VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], result.get("samples"), result.get("error")),
        )
    return result


# A beach's result for a day: the geometric mean of its sites' counts (a count of 0 is
# taken as 1, so the mean is defined).
DAILY = """
SELECT beach_id, day, exp(avg(ln(greatest(ecoli, 1)))) AS geomean, count(*) AS sites
FROM normalized.beach_samples GROUP BY beach_id, day
"""


def status(conn, today: date) -> dict:
    """Each beach with its position and latest RECENT sampling days, and whether the
    latest sample is from this season's current week."""
    beaches = conn.execute(
        """SELECT beach_id, min(beach_name) AS name, avg(lon) AS lon, avg(lat) AS lat
           FROM normalized.beach_samples GROUP BY beach_id ORDER BY 2"""
    ).fetchall()
    days = conn.execute(
        f"""SELECT beach_id, day, geomean, sites FROM (
                SELECT d.*, row_number() OVER (PARTITION BY beach_id ORDER BY day DESC) AS n
                FROM ({DAILY}) d) x
            WHERE n <= %s ORDER BY beach_id, day DESC""",
        (RECENT,),
    ).fetchall()
    by_beach = {}
    for d in days:
        by_beach.setdefault(d["beach_id"], []).append(
            {
                "day": d["day"],
                "ecoli": round(float(d["geomean"])),
                "safe": round(float(d["geomean"])) <= UNSAFE,
            }  # fmt: skip
        )
    latest = max((d["day"] for d in days), default=None)
    return {
        "threshold": UNSAFE,
        "latest": latest,
        "in_season": bool(latest and (today - latest).days <= 3),
        "beaches": [
            {"id": b["beach_id"], "name": b["name"], "lon": round(b["lon"], 5),
             "lat": round(b["lat"], 5), "recent": by_beach.get(b["beach_id"], [])}
            for b in beaches
        ],
    }  # fmt: skip
