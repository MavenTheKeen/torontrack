"""The site's own availability, measured from outside the web process (docs/alerts.md).

Once a minute the alerts process requests a live data endpoint through the public
address, so the proxy, the web process and the database are all on the path, and records
whether it answered. Time counts as available when a check succeeded and the check
before it was at most GAP ago; a failed check, or a silence longer than GAP (the server
or the monitor itself was down), counts as unavailable.
"""

import time
from datetime import datetime, timedelta

import httpx

CHECK_PATH = "/api/lines"
TIMEOUT_SECONDS = 10
GAP = timedelta(minutes=3)
KEEP = timedelta(days=120)
DAYS_SHOWN = 90


def record(conn, client: httpx.Client, site_url: str, now: datetime) -> bool:
    """Check the site once and store the result."""
    ok, ms = False, None
    started = time.monotonic()
    try:
        response = client.get(site_url.rstrip("/") + CHECK_PATH, timeout=TIMEOUT_SECONDS)
        ok = response.status_code == 200
        ms = round((time.monotonic() - started) * 1000)
    except httpx.HTTPError:
        pass
    conn.execute(
        """INSERT INTO ops.site_checks (checked_at, ok, ms) VALUES (%s, %s, %s)
           ON CONFLICT (checked_at) DO NOTHING""",
        (now, ok, ms),
    )
    conn.execute("DELETE FROM ops.site_checks WHERE checked_at < %s", (now - KEEP,))
    return ok


def daily(conn, now: datetime) -> dict:
    """Availability per Toronto day for the last DAYS_SHOWN days, and overall."""
    rows = conn.execute(
        """WITH spans AS (
               SELECT checked_at, ok, ms,
                      checked_at - lag(checked_at) OVER (ORDER BY checked_at) AS span
               FROM ops.site_checks
               WHERE checked_at >= %(since)s
           )
           SELECT (checked_at AT TIME ZONE 'America/Toronto')::date AS day,
                  coalesce(sum(extract(epoch FROM span))
                      FILTER (WHERE ok AND span <= %(gap)s), 0)::float AS up,
                  coalesce(sum(extract(epoch FROM span))
                      FILTER (WHERE NOT ok OR span > %(gap)s), 0)::float AS down,
                  (percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) FILTER (WHERE ok))::int
                      AS median_ms
           FROM spans
           WHERE span IS NOT NULL
           GROUP BY 1
           ORDER BY 1""",
        {"since": now - timedelta(days=DAYS_SHOWN), "gap": GAP},
    ).fetchall()
    days = [
        {
            "day": r["day"].isoformat(),
            "pct": round(100 * r["up"] / (r["up"] + r["down"]), 2),
            "down_minutes": round(r["down"] / 60),
            "median_ms": r["median_ms"],
        }
        for r in rows
        if r["up"] + r["down"] > 0
    ]
    up, down = sum(r["up"] for r in rows), sum(r["down"] for r in rows)
    return {
        "available": bool(days),
        "since": days[0]["day"] if days else None,
        "pct": round(100 * up / (up + down), 2) if up + down else None,
        "days": days,
    }
