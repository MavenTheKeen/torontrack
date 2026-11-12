"""Findings: what the data collected here shows when sources are put side by side.

Each finding is computed from the database on request (cached for an hour by the web app)
and returns a headline, the numbers behind it, a chart and how it was worked out. A
finding whose data is missing or too thin is left out rather than shown with a weak
number. Weather comes from ECCC's hourly observations at Toronto City (6158355), summed
into Toronto calendar days; a day counts only with at least 20 reported hours.
"""

import logging
from datetime import date, timedelta

from transit import food_licences

log = logging.getLogger(__name__)

WEATHER_DAYS = """
SELECT (observed_at AT TIME ZONE 'America/Toronto')::date AS day,
       max(temp_c) AS tmax, min(temp_c) AS tmin, sum(precip_mm) AS precip,
       coalesce(sum(precip_mm) FILTER (WHERE temp_c <= 0.5), 0) AS snow
FROM normalized.weather_hourly
GROUP BY 1
HAVING count(*) >= 20 AND count(precip_mm) >= 20
"""
WET_MM = 1.0  # A wet day: at least 1 mm of precipitation.
SNOW_MM = 2.0  # A snowy day: at least 2 mm fell at or below 0.5 °C.
BAND_C = 5
MIN_DAYS = 3  # Fewest days behind any compared average.


ACRONYMS = {"PAA", "OPTO", "TTC", "ATC", "CBTC", "SRT", "CCTV", "TR", "PA", "EMS", "TPS"}


def _cause(text: str) -> str:
    """'PAA - NO TROUBLE FOUND' -> 'PAA - no trouble found': sentence case, keeping the
    TTC's acronyms."""
    words = []
    for i, word in enumerate(text.split(" ")):
        letters = "".join(c for c in word if c.isalpha())
        word = word if letters in ACRONYMS else word.lower()
        words.append(word[:1].upper() + word[1:] if i == 0 else word)
    return " ".join(words)


def _pct(value: float) -> int:
    return round(100 * value)


def bikeshare_rain(conn) -> dict | None:
    """Bike Share trips per day against the day's high temperature, dry and wet."""
    rows = conn.execute(
        f"""WITH w AS ({WEATHER_DAYS}),
                b AS (SELECT trip_date AS day, sum(departures) AS trips
                      FROM normalized.bikeshare_trip_station_days GROUP BY 1)
            SELECT floor(w.tmax / {BAND_C})::int * {BAND_C} AS band, w.precip >= %s AS wet,
                   extract(isodow FROM b.day) < 6 AS weekday, count(*) AS days,
                   avg(b.trips) AS trips, min(b.day) AS first, max(b.day) AS last
            FROM b JOIN w USING (day) GROUP BY 1, 2, 3""",
        (WET_MM,),
    ).fetchall()
    cells = {(r["band"], r["wet"], r["weekday"]): r for r in rows}
    # Compare wet and dry days within the same temperature band and kind of day, weighted
    # by the wet days, so a rainy autumn is not compared with a dry July.
    weighted = weight = 0.0
    for (band, wet, weekday), r in cells.items():
        dry = cells.get((band, False, weekday))
        if wet and dry and r["days"] >= MIN_DAYS and dry["days"] >= MIN_DAYS:
            weighted += r["days"] * float(r["trips"] / dry["trips"])
            weight += r["days"]
    if weight < 30:
        return None
    bands = sorted({b for b, _, _ in cells})
    series = []
    for wet, name in ((False, "Dry days"), (True, f"Wet days ({WET_MM:g} mm or more)")):
        values = []
        for band in bands:
            parts = [r for (b, w, _), r in cells.items() if b == band and w == wet]
            days = sum(r["days"] for r in parts)
            values.append(
                round(sum(float(r["trips"]) * r["days"] for r in parts) / days)
                if days >= MIN_DAYS
                else None
            )
        series.append({"name": name, "values": values})
    dry = dict(zip(bands, series[0]["values"], strict=True))
    if not dry.get(0) or not dry.get(25):
        return None
    fewer = _pct(1 - weighted / weight)
    first = min(r["first"] for r in rows)
    last = max(r["last"] for r in rows)
    return {
        "id": "bikeshare-weather",
        "topic": "Bike Share",
        "title": f"A wet day costs Bike Share about {fewer}% of its trips",
        "summary": (
            f"At the same temperature and on the same kind of day, days with {WET_MM:g} mm "
            f"of rain or more had {fewer}% fewer trips than dry days. Temperature matters "
            f"more: a dry day with a high of 25 to 30 °C averaged {dry[25] / dry[0]:.1f} "
            f"times the trips of a dry day with a high of 0 to 5 °C ({dry[25]:,} against "
            f"{dry[0]:,})."
        ),
        "chart": {
            "kind": "lines",
            "x": [f"{b} to {b + BAND_C}" for b in bands],
            "x_label": "Day's high, °C",
            "y_label": "Trips per day",
            "series": series,
        },
        "method": (
            "Trips per day from Bike Share Toronto's ridership files, joined to that day's "
            "high temperature and total precipitation. Wet and dry days are compared within "
            f"each {BAND_C} °C band, separately for weekdays and weekends, and the ratios "
            "averaged by the number of wet days. Ridership grew over the period, which "
            "this comparison does not remove."
        ),
        "period": [first, last],
        "sources": ["Bike Share Toronto ridership", "ECCC hourly weather"],
    }


def snow_delays(conn) -> dict | None:
    """Subway delays in the TTC's own log on snowy winter days against other winter days."""
    rows = conn.execute(
        f"""WITH w AS ({WEATHER_DAYS}),
                d AS (SELECT (delay_at AT TIME ZONE 'America/Toronto')::date AS day,
                             count(*) FILTER (WHERE min_delay > 0) AS delays,
                             sum(min_delay) AS minutes
                      FROM normalized.ttc_official_delays GROUP BY 1)
            SELECT w.snow >= %s AS snowy, count(*) AS days, avg(d.delays) AS delays,
                   avg(d.minutes) AS minutes, min(d.day) AS first, max(d.day) AS last
            FROM d JOIN w USING (day)
            WHERE extract(month FROM d.day) IN (12, 1, 2, 3)
            GROUP BY 1""",
        (SNOW_MM,),
    ).fetchall()
    by = {r["snowy"]: r for r in rows}
    if True not in by or False not in by or by[True]["days"] < 10:
        return None
    snowy, other = by[True], by[False]
    more = _pct(float(snowy["minutes"] / other["minutes"]) - 1)
    return {
        "id": "snow-delays",
        "topic": "Subway",
        "title": f"Snow makes subway delays longer, not more frequent: {more}% more delay time",
        "summary": (
            f"On {snowy['days']} snowy winter days the TTC logged "
            f"{float(snowy['delays']):.0f} subway delays a day, close to the "
            f"{float(other['delays']):.0f} on {other['days']} other winter days, but they "
            f"added up to {float(snowy['minutes']):.0f} minutes against "
            f"{float(other['minutes']):.0f}."
        ),
        "chart": {
            "kind": "bars",
            "x": ["Other winter days", "Snowy days"],
            "y_label": "Delay minutes per day",
            "series": [
                {
                    "name": "Delay minutes per day",
                    "values": [round(float(other["minutes"])), round(float(snowy["minutes"]))],
                }
            ],  # fmt: skip
        },
        "method": (
            "The TTC's published subway delay log, December to March. A snowy day had at "
            f"least {SNOW_MM:g} mm of precipitation in hours at or below 0.5 °C at the "
            "downtown weather station. Delays of zero minutes are not counted as delays."
        ),
        "period": [min(r["first"] for r in rows), max(r["last"] for r in rows)],
        "sources": ["TTC subway delay data", "ECCC hourly weather"],
    }


def delay_causes(conn) -> dict | None:
    """The causes behind the most subway delay minutes in the TTC's log."""
    rows = conn.execute(
        """SELECT coalesce(c.description, d.code, 'No code') AS cause,
                  sum(d.min_delay) AS minutes, count(*) AS delays,
                  min(d.delay_at) AS first, max(d.delay_at) AS last
           FROM normalized.ttc_official_delays d
           LEFT JOIN normalized.ttc_delay_codes c USING (code)
           WHERE d.min_delay > 0
           GROUP BY 1 ORDER BY 2 DESC"""
    ).fetchall()
    if len(rows) < 8:
        return None
    total = sum(r["minutes"] for r in rows)
    top = rows[:8]
    lead = top[0]
    return {
        "id": "delay-causes",
        "topic": "Subway",
        "title": f"{_cause(lead['cause'])} costs the subway more time than any other cause",
        "summary": (
            f"Of {total:,} minutes of subway delay logged, {_pct(lead['minutes'] / total)}% "
            f"were put down to {lead['cause'].lower()} ({lead['delays']:,} delays). The eight "
            f"largest causes account for {_pct(sum(r['minutes'] for r in top) / total)}%."
        ),
        "chart": {
            "kind": "hbars",
            "x": [_cause(r["cause"]) for r in top],
            "y_label": "Delay minutes",
            "series": [{"name": "Delay minutes", "values": [r["minutes"] for r in top]}],
        },
        "method": (
            "Every subway delay in the TTC's published log with a delay above zero minutes, "
            "summed by the TTC's own cause code and its description."
        ),
        "period": [min(r["first"] for r in rows).date(), max(r["last"] for r in rows).date()],
        "sources": ["TTC subway delay data"],
    }


POTHOLE = "request_type ILIKE '%%pot hole%%' OR request_type ILIKE '%%pothole%%'"


def potholes(conn) -> dict | None:
    """Pothole reports to 311 by month against freeze-thaw days in the month before."""
    rows = conn.execute(
        f"""WITH w AS ({WEATHER_DAYS}),
                ft AS (SELECT date_trunc('month', day)::date AS month,
                              count(*) FILTER (WHERE tmin < 0 AND tmax > 0) AS cycles,
                              count(*) AS days
                       FROM w GROUP BY 1),
                p AS (SELECT date_trunc('month', day)::date AS month, sum(requests) AS reports
                      FROM normalized.service_requests_daily WHERE {POTHOLE} GROUP BY 1)
            SELECT p.month, p.reports, ft.cycles, ft.days
            FROM p JOIN ft ON ft.month = p.month - interval '1 month'
            WHERE ft.days >= 25
            ORDER BY 1"""
    ).fetchall()
    if len(rows) < 24:
        return None
    xs = [float(r["cycles"]) for r in rows]
    ys = [float(r["reports"]) for r in rows]
    n = len(rows)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    r = sxy / (sxx * syy) ** 0.5
    by_month = {}
    for row in rows:
        by_month.setdefault(row["month"].month, []).append(float(row["reports"]))
    months = sorted(by_month)
    averages = [round(sum(by_month[m]) / len(by_month[m])) for m in months]
    strength = "closely" if r >= 0.7 else "clearly" if r >= 0.5 else "loosely"
    peak = months[averages.index(max(averages))]
    low = months[averages.index(min(averages))]
    names = [date(2000, m, 1).strftime("%b") for m in months]
    return {
        "id": "potholes",
        "topic": "Streets",
        "title": f"Pothole reports peak in {date(2000, peak, 1):%B}, after the freeze-thaw",
        "summary": (
            f"311 pothole reports average {max(averages):,} in {date(2000, peak, 1):%B} and "
            f"{min(averages):,} in {date(2000, low, 1):%B}. Month by month, reports "
            f"{strength} track "
            f"the number of days in the month before that froze and thawed (correlation "
            f"{r:.2f} over {n} months)."
        ),
        "chart": {
            "kind": "bars",
            "x": names,
            "y_label": "Pothole reports per month",
            "series": [{"name": "Average reports", "values": averages}],
        },
        "method": (
            "311 service requests whose type mentions potholes, counted by month, against "
            "the number of days in the previous month whose low was below 0 °C and high "
            "above it at the downtown weather station. The correlation is Pearson's r. A "
            "report is a request, not a measured pothole: several can describe one hole."
        ),
        "period": [rows[0]["month"], rows[-1]["month"]],
        "sources": ["City of Toronto 311 service requests", "ECCC hourly weather"],
    }


AGE_BANDS = ((0, 5, "Under 5 years"), (5, 20, "5 to 19 years"), (20, 200, "20 years or more"))


def food_age(conn, today: date) -> dict | None:
    """Inspection results by how long a premises has been licensed at its address."""
    since = food_licences.since_listed(conn)
    if len(since) < 1000:
        return None
    rows = conn.execute(
        """SELECT premises_id, count(*) AS inspections,
                  count(*) FILTER (WHERE status <> 'Pass') AS failed
           FROM normalized.food_inspections WHERE inspected_on >= %s
           GROUP BY 1""",
        (today - timedelta(days=730),),
    ).fetchall()
    totals = {label: [0, 0, 0] for _, _, label in AGE_BANDS}  # premises, inspections, failed
    for r in rows:
        first = since.get(r["premises_id"])
        if not first:
            continue
        years = (today - first).days / 365.25
        for low, high, label in AGE_BANDS:
            if low <= years < high:
                t = totals[label]
                t[0] += 1
                t[1] += r["inspections"]
                t[2] += r["failed"]
    labels = [label for _, _, label in AGE_BANDS]
    if any(totals[label][1] < 500 for label in labels):
        return None
    rates = [totals[label][2] / totals[label][1] for label in labels]
    young, old = rates[0], rates[-1]
    if abs(young - old) < 0.02:
        title = "Long-running places pass inspections about as often as new ones"
    elif old < young:
        title = (
            f"Places open 20 years or more fail {_pct(old)}% of inspections, "
            f"newer ones {_pct(young)}%"
        )
    else:
        title = f"Places open 20 years or more fail more often: {_pct(old)}% against {_pct(young)}%"
    return {
        "id": "food-age",
        "topic": "Food",
        "title": title,
        "summary": (
            f"Over the last two years, {100 * young:.1f}% of inspections at places licensed at "
            f"their address for under five years ended in a conditional pass or a closure, "
            f"against {100 * rates[1]:.1f}% for 5 to 19 years and {100 * old:.1f}% for 20 years or "
            f"more ({totals[labels[-1]][0]:,} places)."
        ),
        "chart": {
            "kind": "bars",
            "x": labels,
            "y_label": "Inspections not passed, %",
            "series": [{"name": "Not passed", "values": [round(100 * x, 1) for x in rates]}],
        },
        "method": (
            "DineSafe inspections of the last two years at premises matched to a business "
            "licence at the same address under a similar name; age is from the earliest such "
            "licence. A result other than Pass counts as not passed. Places that closed are "
            "missing, so older places are survivors: this shows a difference, not a cause."
        ),
        "period": [today - timedelta(days=730), today],
        "sources": ["Toronto Public Health DineSafe", "City of Toronto business licences"],
    }


def all_findings(conn, today: date) -> list[dict]:
    found = []
    for build in (
        lambda: bikeshare_rain(conn),
        lambda: snow_delays(conn),
        lambda: delay_causes(conn),
        lambda: potholes(conn),
        lambda: food_age(conn, today),
    ):
        try:
            result = build()
        except Exception:  # One broken finding must not take the page down.
            log.exception("Finding failed")
            conn.rollback()
            continue
        if result:
            found.append(result)
    return found
