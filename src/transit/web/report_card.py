"""The monthly report card: each finished month's measurements, graded by excess wait time
(report.py), with the change from the month before and TTC's own delay log once published.

The grades are this project's, not the TTC's. They use one scale for every mode, because a
rider's extra wait costs the same on any vehicle.
"""

import re
from datetime import date, datetime

from transit.web.report import MEASURES, ROUTE_NAMES, SUBWAY_ONLY, TORONTO, _pct, _rows

# Most extra wait, in seconds, for each grade; beyond the last is an F.
GRADES = [(30, "A"), (60, "B"), (120, "C"), (240, "D")]
# Fewer measured days than this are too few to grade.
MIN_DAYS = 10
ROUTE_MIN_GAPS = 300
ROUTES_SHOWN = 5
MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def grade(ewt_seconds: int | None) -> str | None:
    if ewt_seconds is None:
        return None
    return next((letter for limit, letter in GRADES if ewt_seconds <= limit), "F")


def bounds(month: str) -> tuple[date, date]:
    """The first day of the month and of the next."""
    year, number = map(int, month.split("-"))
    return date(year, number, 1), date(year + number // 12, number % 12 + 1, 1)


def months(conn, now: datetime) -> list[str]:
    """Finished months with measurements, newest first."""
    first_of_month = now.astimezone(TORONTO).date().replace(day=1)
    rows = conn.execute(
        """SELECT DISTINCT to_char(service_date, 'YYYY-MM') AS month
           FROM analytics.ttc_headway_reliability_hourly
           WHERE service_date < %s ORDER BY 1 DESC""",
        (first_of_month,),
    ).fetchall()
    return [r["month"] for r in rows]


def _lines(conn, start: date, end: date) -> list[dict]:
    rows = _rows(
        conn,
        f"""SELECT route_id, count(DISTINCT service_date)::int AS days, {MEASURES}
            FROM analytics.ttc_headway_reliability_hourly
            WHERE service_date >= %(start)s AND service_date < %(end)s
              AND compared_headways > 0 AND {SUBWAY_ONLY}
            GROUP BY 1 ORDER BY route_id::int""",
        {"start": start, "end": end},
    )
    return [
        {**r, "grade": grade(r["ewt_seconds"]) if r["days"] >= MIN_DAYS else None} for r in rows
    ]


def _surface(conn, start: date, end: date) -> dict | None:
    sums = """count(DISTINCT service_date)::int AS days,
              sum(headways)::int AS headways,
              sum(bunched)::int AS bunched,
              sum(long_gaps)::int AS long_gaps,
              round((sum(compared_squared_seconds) - sum(compared_scheduled_product))
                    / nullif(2 * sum(compared_seconds), 0))::int AS ewt_seconds"""
    where = "service_date >= %(start)s AND service_date < %(end)s"
    params = {"start": start, "end": end, "min": ROUTE_MIN_GAPS, "shown": ROUTES_SHOWN}
    overall = conn.execute(
        f"""SELECT count(DISTINCT route_id)::int AS routes, {sums}
            FROM analytics.ttc_surface_reliability_daily WHERE {where}""",
        params,
    ).fetchone()
    if not overall["headways"]:
        return None

    def graded(row):
        return {
            **row,
            "bunched_pct": _pct(row["bunched"], row["headways"], 1),
            "long_pct": _pct(row["long_gaps"], row["headways"], 1),
            "grade": grade(row["ewt_seconds"]) if row["days"] >= MIN_DAYS else None,
        }

    ranked = conn.execute(
        f"""SELECT d.route_id, r.short_name, r.long_name, coalesce(r.mode, 'bus') AS mode, {sums}
            FROM analytics.ttc_surface_reliability_daily d {ROUTE_NAMES}
            WHERE {where}
            GROUP BY 1, 2, 3, 4 HAVING sum(headways) >= %(min)s
            ORDER BY ewt_seconds, d.route_id""",
        params,
    ).fetchall()
    shown = min(ROUTES_SHOWN, len(ranked) // 2)
    return {
        **graded(overall),
        "best": [graded(r) for r in ranked[:shown]],
        "worst": [graded(r) for r in ranked[::-1][:shown]],
    }


def _service_seen(conn, start: date, end: date) -> dict | None:
    row = conn.execute(
        """SELECT count(DISTINCT service_date)::int AS days,
                  round(100 * sum(delivered * scheduled_trips)
                        / nullif(sum(scheduled_trips), 0), 1) AS seen_pct,
                  sum(empty_hours)::int AS empty_hours
           FROM analytics.ttc_surface_delivery_daily
           WHERE stops >= 3 AND service_date >= %(start)s AND service_date < %(end)s
             AND ((service_date + 2)::timestamp AT TIME ZONE 'America/Toronto')
                 - (service_date::timestamp AT TIME ZONE 'America/Toronto')
                 = interval '48 hours'""",
        {"start": start, "end": end},
    ).fetchone()
    return row if row["days"] else None


def _elevators(conn, start: date, end: date) -> dict:
    return conn.execute(
        """SELECT count(*)::int AS outages,
                  round(percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_minutes)
                        ::numeric)::int AS median_minutes
           FROM analytics.ttc_elevator_outages
           WHERE NOT began_before_collection
             AND (first_seen_at AT TIME ZONE 'America/Toronto')::date >= %(start)s
             AND (first_seen_at AT TIME ZONE 'America/Toronto')::date < %(end)s""",
        {"start": start, "end": end},
    ).fetchone()


def _official(conn, start: date, end: date) -> dict:
    """TTC's delay log for the month, which it publishes some time after the month ends."""
    through = conn.execute(
        """SELECT (max(delay_at) AT TIME ZONE 'America/Toronto')::date AS d
           FROM normalized.ttc_official_delays"""
    ).fetchone()["d"]
    if through is None or (end - through).days > 1:
        return {"published": False, "through": through}
    rows = conn.execute(
        """SELECT route_id, count(*)::int AS incidents,
                  count(*) FILTER (WHERE in_coverage)::int AS checked,
                  count(*) FILTER (WHERE in_coverage AND detected)::int AS seen,
                  sum(min_delay)::int AS delay_minutes
           FROM analytics.ttc_delay_detection
           WHERE service_date >= %(start)s AND service_date < %(end)s AND route_id IS NOT NULL
           GROUP BY 1 ORDER BY route_id::int""",
        {"start": start, "end": end},
    ).fetchall()
    return {
        "published": True,
        "through": through,
        "lines": [{**r, "seen_pct": _pct(r["seen"], r["checked"])} for r in rows],
    }


def card(conn, month: str) -> dict | None:
    """The month's card; None if the month was not measured."""
    start, end = bounds(month)
    lines = _lines(conn, start, end)
    if not lines:
        return None
    previous = {r["route_id"]: r for r in _lines(conn, bounds(_month_before(start))[0], start)}
    gaps = conn.execute(
        """SELECT coalesce(round(sum(gap_seconds) / 3600.0, 1), 0) AS hours
           FROM analytics.ttc_collection_gaps
           WHERE gap_seconds > 1800
             AND (gap_start AT TIME ZONE 'America/Toronto')::date >= %(start)s
             AND (gap_start AT TIME ZONE 'America/Toronto')::date < %(end)s""",
        {"start": start, "end": end},
    ).fetchone()
    days = conn.execute(
        """SELECT count(DISTINCT service_date)::int AS days, min(service_date) AS first_day,
                  max(service_date) AS last_day
           FROM analytics.ttc_headway_reliability_hourly
           WHERE service_date >= %(start)s AND service_date < %(end)s""",
        {"start": start, "end": end},
    ).fetchone()
    return {
        "month": month,
        "coverage": {
            **days,
            "days_in_month": (end - start).days,
            "not_collected_hours": gaps["hours"],
        },
        "grades": [{"grade": letter, "up_to_seconds": limit} for limit, letter in GRADES],
        "min_days": MIN_DAYS,
        "lines": [
            {
                **line,
                "previous_ewt_seconds": (previous.get(line["route_id"]) or {}).get("ewt_seconds"),
                "previous_grade": (previous.get(line["route_id"]) or {}).get("grade"),
            }
            for line in lines
        ],
        "surface": _surface(conn, start, end),
        "service_seen": _service_seen(conn, start, end),
        "elevators": _elevators(conn, start, end),
        "official": _official(conn, start, end),
    }


def _month_before(first: date) -> str:
    return f"{first.year - 1}-12" if first.month == 1 else f"{first.year}-{first.month - 1:02d}"
