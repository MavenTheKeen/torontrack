"""The reliability report: every measured subway gap since collection began, summarised by
line, day type, time of day, station and weather.

The headline measure is excess wait time (EWT): how much longer than the schedule
promises a rider who turns up at a random moment waits on average. Uneven gaps raise it
even when trains run as often as scheduled, which a plain average misses. The dbt
models store sums, so EWT is combined here over any grouping as
(sum(h^2) - sum(h * s)) / (2 sum(h)).
"""

from zoneinfo import ZoneInfo

from transit.web import bikes

TORONTO = ZoneInfo("America/Toronto")
# Lines 5 and 6 have no live arrivals; the few recorded leave gaps of many hours.
SUBWAY_ONLY = "route_id IN ('1', '2', '4')"
# Names and modes from the newest schedule; a route since dropped keeps its number.
ROUTE_NAMES = """LEFT JOIN normalized.ttc_surface_routes r
              ON r.route_id = d.route_id AND r.feed_version = (
                  SELECT feed_version FROM normalized.ttc_gtfs_versions
                  ORDER BY loaded_at DESC LIMIT 1)"""

# Hours of the service day (24+ is after midnight) grouped as riders think of the day.
PERIODS = [
    ("early", "Early morning", "before 7 a.m.", 0, 6),
    ("am_peak", "Morning rush", "7 to 10 a.m.", 7, 9),
    ("midday", "Midday", "10 a.m. to 3 p.m.", 10, 14),
    ("pm_peak", "Afternoon rush", "3 to 7 p.m.", 15, 18),
    ("evening", "Evening", "7 to 10 p.m.", 19, 21),
    ("late", "Late night", "after 10 p.m.", 22, 99),
]
PERIOD_SQL = (
    "CASE "
    + " ".join(
        f"WHEN service_hour BETWEEN {low} AND {high} THEN '{key}'"
        for key, _, _, low, high in PERIODS
    )
    + " END"
)
# A station needs this many measured gaps before it is ranked, so a quiet platform with
# one bad gap does not top the list.
STATION_MIN_GAPS = 150
STATIONS_SHOWN = 10
# Bus and streetcar routes need this many measured headways before they are ranked.
ROUTE_MIN_HEADWAYS = 300
ROUTES_SHOWN = 15
# A route is compared with and without road restrictions once it has this many weekdays
# of each.
CLOSURE_MIN_DAYS = 3
# Scheduled service seen on the live feed. One or two stops are too few to trust: a single
# stop near a terminal can read near zero on a route that ran normally.
DELIVERY_MIN_STOPS = 3
DELIVERY_DAYS = 14
DELIVERY_MIN_DAYS = 7
DELIVERY_MIN_TRIPS = 100
DELIVERY_DROP = 0.25
DELIVERY_SHOWN = 8

MEASURES = """
    sum(compared_headways)::int AS gaps,
    sum(regular_headways)::int AS regular,
    sum(long_gaps)::int AS long_gaps,
    round((sum(compared_squared_seconds) - sum(compared_scheduled_product))
          / nullif(2 * sum(compared_seconds), 0))::int AS ewt_seconds,
    round(sum(compared_squared_seconds) / nullif(2 * sum(compared_seconds), 0))::int
        AS wait_seconds,
    round(sum(compared_scheduled_product) / nullif(2 * sum(compared_seconds), 0))::int
        AS scheduled_wait_seconds"""


def _rows(conn, sql: str, params=None) -> list[dict]:
    return [
        {
            **r,
            "regular_pct": _pct(r["regular"], r["gaps"]),
            "long_pct": _pct(r["long_gaps"], r["gaps"], 1),
        }
        for r in conn.execute(sql, params or {}).fetchall()
    ]


def _pct(part, whole, digits=0):
    return round(100 * part / whole, digits) if whole else None


def delivery(conn) -> dict | None:
    """Scheduled bus and streetcar service seen on the live feed over the last completed
    days; None until dbt has built the model or while nothing is measurable."""
    if (
        conn.execute("SELECT to_regclass('analytics.ttc_surface_delivery_daily') AS t").fetchone()[
            "t"
        ]
        is None
    ):
        return None
    params = {
        "stops": DELIVERY_MIN_STOPS,
        "days": DELIVERY_DAYS,
        "min_days": DELIVERY_MIN_DAYS,
        "min_trips": DELIVERY_MIN_TRIPS,
        "drop": DELIVERY_DROP,
        "shown": DELIVERY_SHOWN,
    }
    recent = """recent AS (
        SELECT * FROM analytics.ttc_surface_delivery_daily
        WHERE stops >= %(stops)s
          AND service_date < ((now() AT TIME ZONE 'America/Toronto') - interval '4 hours')::date
          -- Hours counted from midnight are off by one on a day the clocks change, and the
          -- night service of the day before runs through the change.
          AND ((service_date + 2)::timestamp AT TIME ZONE 'America/Toronto')
              - (service_date::timestamp AT TIME ZONE 'America/Toronto') = interval '48 hours'
          AND service_date > (
              SELECT max(service_date) FROM analytics.ttc_surface_delivery_daily
              WHERE stops >= %(stops)s
                AND service_date < ((now() AT TIME ZONE 'America/Toronto')
                                    - interval '4 hours')::date
          ) - %(days)s
    )"""
    overall = conn.execute(
        f"""WITH {recent}
            SELECT min(service_date) AS first_day, max(service_date) AS last_day,
                   count(DISTINCT service_date) AS days, count(DISTINCT route_id) AS routes,
                   round(100 * (1 - sum(delivered * scheduled_trips)
                                    / nullif(sum(scheduled_trips), 0)), 1) AS missing_pct,
                   sum(empty_hours)::int AS empty_hours
            FROM recent""",
        params,
    ).fetchone()
    if not overall["days"]:
        return None
    ranked = conn.execute(
        f"""WITH {recent}
            SELECT d.route_id, r.short_name, r.long_name, coalesce(r.mode, 'bus') AS mode,
                   round(100 * sum(d.delivered * d.scheduled_trips)
                         / sum(d.scheduled_trips))::int AS seen_pct,
                   round(sum((1 - d.delivered) * d.scheduled_trips)
                         / count(DISTINCT d.service_date))::int AS missing_per_day,
                   sum(d.empty_hours)::int AS empty_hours
            FROM recent d {ROUTE_NAMES}
            GROUP BY 1, 2, 3, 4
            HAVING count(DISTINCT d.service_date) >= %(min_days)s
               AND sum(d.scheduled_trips) >= %(min_trips)s
            ORDER BY seen_pct, missing_per_day DESC, d.route_id LIMIT %(shown)s""",
        params,
    ).fetchall()
    # A day far below the route's own usual level, which a steady measurement bias cannot explain.
    worst_days = conn.execute(
        f"""WITH {recent},
            by_day AS (
                SELECT service_date, route_id,
                       (sum(delivered * scheduled_trips) / sum(scheduled_trips))::float8 AS share,
                       sum(scheduled_trips) AS scheduled
                FROM recent GROUP BY 1, 2
            ), usual AS (
                SELECT route_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY share) AS share
                FROM by_day GROUP BY 1 HAVING count(*) >= %(min_days)s
            )
            SELECT d.service_date, d.route_id, r.short_name, r.long_name,
                   coalesce(r.mode, 'bus') AS mode,
                   round(100 * d.share)::int AS seen_pct, round(100 * u.share)::int AS usual_pct
            FROM by_day d JOIN usual u USING (route_id) {ROUTE_NAMES}
            WHERE u.share - d.share >= %(drop)s
            ORDER BY (u.share - d.share) * d.scheduled DESC, d.service_date, d.route_id
            LIMIT %(shown)s""",
        params,
    ).fetchall()
    return {**overall, "routes_ranked": ranked, "worst_days": worst_days}


def surface(conn) -> dict | None:
    """Bus and streetcar reliability on frequent service, by route; None until dbt has
    built the model."""
    if (
        conn.execute(
            "SELECT to_regclass('analytics.ttc_surface_reliability_daily') AS t"
        ).fetchone()["t"]
        is None
    ):
        return None
    sums = """
        sum(headways)::int AS headways,
        sum(bunched)::int AS bunched,
        sum(long_gaps)::int AS long_gaps,
        round((sum(compared_squared_seconds) - sum(compared_scheduled_product))
              / nullif(2 * sum(compared_seconds), 0))::int AS ewt_seconds,
        round(sum(compared_squared_seconds) / nullif(2 * sum(compared_seconds), 0))::int
            AS wait_seconds,
        round(sum(compared_scheduled_product) / nullif(2 * sum(compared_seconds), 0))::int
            AS scheduled_wait_seconds"""
    table = "analytics.ttc_surface_reliability_daily"
    overall = conn.execute(
        f"""SELECT min(service_date) AS first_day, max(service_date) AS last_day,
                   count(DISTINCT service_date) AS days, count(DISTINCT route_id) AS routes,
                   {sums}
            FROM {table}"""
    ).fetchone()
    if not overall["headways"]:
        return {**overall, "routes_ranked": [], "route_min_headways": ROUTE_MIN_HEADWAYS}

    def with_pct(r):
        return {
            **r,
            "bunched_pct": _pct(r["bunched"], r["headways"], 1),
            "long_pct": _pct(r["long_gaps"], r["headways"], 1),
        }

    ranked = conn.execute(
        f"""SELECT d.route_id, r.short_name, r.long_name, coalesce(r.mode, 'bus') AS mode,
                   {sums}
            FROM {table} d
            {ROUTE_NAMES}
            GROUP BY 1, 2, 3, 4 HAVING sum(headways) >= %(min)s
            ORDER BY ewt_seconds DESC NULLS LAST LIMIT %(shown)s""",
        {"min": ROUTE_MIN_HEADWAYS, "shown": ROUTES_SHOWN},
    ).fetchall()
    return {
        **with_pct(overall),
        "routes_ranked": [with_pct(r) for r in ranked],
        "route_min_headways": ROUTE_MIN_HEADWAYS,
        "closures": closures(conn, sums, with_pct),
        "delivery": delivery(conn),
    }


def closures(conn, sums: str, with_pct) -> dict | None:
    """Weekday reliability with and without a high-impact road restriction along the
    route, pooled over routes with CLOSURE_MIN_DAYS of each."""
    if (
        conn.execute("SELECT to_regclass('analytics.ttc_surface_closure_effect') AS t").fetchone()[
            "t"
        ]
        is None
    ):
        return None
    pooled = conn.execute(
        f"""WITH comparable AS (
                SELECT route_id, direction_id FROM analytics.ttc_surface_closure_effect
                GROUP BY 1, 2
                HAVING min(days) FILTER (WHERE affected) >= %(min)s
                   AND min(days) FILTER (WHERE NOT affected) >= %(min)s)
            SELECT affected, count(DISTINCT (route_id, direction_id)) AS route_directions,
                   sum(days)::int AS route_days, {sums}
            FROM analytics.ttc_surface_closure_effect JOIN comparable
              USING (route_id, direction_id)
            GROUP BY affected""",
        {"min": CLOSURE_MIN_DAYS},
    ).fetchall()
    sides = {("with" if r["affected"] else "without"): with_pct(r) for r in pooled}
    return (
        {"min_days": CLOSURE_MIN_DAYS, **sides}
        if len(sides) == 2
        else {"min_days": CLOSURE_MIN_DAYS}
    )


def report(conn) -> dict:
    hourly = "analytics.ttc_headway_reliability_hourly"
    coverage = conn.execute(
        f"""SELECT min(service_date) AS first_day, max(service_date) AS last_day,
                   count(DISTINCT service_date) AS days,
                   count(DISTINCT service_date) FILTER (WHERE day_type = 'weekday')
                     AS weekdays,
                   (SELECT round(sum(gap_seconds) / 3600.0, 1)
                    FROM analytics.ttc_collection_gaps WHERE gap_seconds > 1800)
                     AS outage_hours
            FROM {hourly}"""
    ).fetchone()
    lines = _rows(
        conn,
        f"""SELECT route_id, CASE WHEN day_type = 'weekday' THEN 'weekday' ELSE 'weekend'
                   END AS days, {MEASURES}
            FROM {hourly} WHERE compared_headways > 0 AND {SUBWAY_ONLY}
            GROUP BY 1, 2 ORDER BY route_id::int, 2""",
    )
    periods = _rows(
        conn,
        f"""SELECT route_id, {PERIOD_SQL} AS period, {MEASURES}
            FROM {hourly} WHERE day_type = 'weekday' AND compared_headways > 0 AND {SUBWAY_ONLY}
            GROUP BY 1, 2 ORDER BY route_id::int, 2""",
    )
    weather = _rows(
        conn,
        f"""SELECT route_id, conditions,
                   count(DISTINCT (service_date, service_hour)) AS hours, {MEASURES}
            FROM {hourly} WHERE conditions IS NOT NULL AND compared_headways > 0 AND {SUBWAY_ONLY}
            GROUP BY 1, 2 ORDER BY route_id::int, 2""",
    )
    daily = _rows(
        conn,
        f"""SELECT service_date, min(day_type) AS day_type, route_id, {MEASURES}
            FROM {hourly} WHERE compared_headways > 0 AND {SUBWAY_ONLY}
            GROUP BY 1, 3 ORDER BY 1, route_id::int""",
    )
    stations = _rows(
        conn,
        f"""SELECT route_id, station_key, station_name, platform, towards, {MEASURES}
            FROM analytics.ttc_station_reliability WHERE {SUBWAY_ONLY}
            GROUP BY 1, 2, 3, 4, 5 HAVING sum(compared_headways) >= %(min)s
            ORDER BY ewt_seconds DESC NULLS LAST LIMIT %(shown)s""",
        {"min": STATION_MIN_GAPS, "shown": STATIONS_SHOWN},
    )
    weather_hours = conn.execute(
        f"""SELECT count(DISTINCT (service_date, service_hour)) FILTER
                     (WHERE conditions IS NOT NULL) AS with_weather,
                   count(DISTINCT (service_date, service_hour)) AS total
            FROM {hourly}"""
    ).fetchone()
    return {
        "coverage": coverage,
        "lines": lines,
        "periods": periods,
        "period_labels": [{"key": k, "label": label, "hours": h} for k, label, h, _, _ in PERIODS],
        "weather": weather,
        "weather_hours": weather_hours,
        "daily": daily,
        "stations": stations,
        "station_min_gaps": STATION_MIN_GAPS,
        "surface": surface(conn),
        "bike_trips": bikes.summary(conn),
    }
