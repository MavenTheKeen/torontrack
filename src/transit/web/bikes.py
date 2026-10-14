"""Read queries for Bike Share ridership: a dock's trips and the network-wide summary.

Ridership comes from the city's monthly open data (transit.bikeshare.trips), so it ends
weeks before today; every response says how far it reaches.
"""

DESTINATIONS_SHOWN = 5
DOCKS_SHOWN = 10
ROUTES_SHOWN = 10

# The newest location and name of every dock in the live feed.
DOCK_PLACES = """
    SELECT DISTINCT ON (station_id) station_id, name, lat, lon
    FROM normalized.station_versions ORDER BY station_id, station_version_id DESC"""


def _coverage(conn) -> dict | None:
    row = conn.execute(
        """SELECT min(first_trip) AS first_trip, max(last_trip) AS last_trip,
                  sum(trips)::bigint AS trips
           FROM normalized.bikeshare_trip_files"""
    ).fetchone()
    return dict(row) if row and row["last_trip"] else None


def dock(conn, station_id: str) -> dict | None:
    """The last 12 months of trips at a dock: totals, weekday hours, top destinations."""
    coverage = _coverage(conn)
    if coverage is None:
        return None
    last = coverage["last_trip"]
    totals = conn.execute(
        """SELECT sum(departures)::int AS departures, sum(arrivals)::int AS arrivals,
                  sum(member_departures)::int AS member_departures,
                  sum(ebike_departures)::int AS ebike_departures,
                  sum(round_trips)::int AS round_trips
           FROM normalized.bikeshare_trip_station_days
           WHERE station_id = %s AND trip_date > %s - 365""",
        (station_id, last),
    ).fetchone()
    if not totals["departures"] and not totals["arrivals"]:
        return None
    hours = conn.execute(
        """SELECT hour, sum(departures)::int AS departures, sum(arrivals)::int AS arrivals
           FROM normalized.bikeshare_trip_station_hours
           WHERE station_id = %s AND day_type = 'weekday'
             AND month > date_trunc('month', %s::date - 365)
           GROUP BY hour ORDER BY hour""",
        (station_id, last),
    ).fetchall()
    year = last.year
    destinations = conn.execute(
        f"""WITH places AS ({DOCK_PLACES})
            SELECT p.end_station_id AS station_id,
                   coalesce(pl.name, t.name, p.end_station_id) AS name, pl.lat, pl.lon,
                   p.trips, round(p.timed_seconds / nullif(p.timed_trips, 0) / 60.0)::int
                       AS typical_minutes
            FROM normalized.bikeshare_trip_pairs p
            LEFT JOIN places pl ON pl.station_id = p.end_station_id
            LEFT JOIN normalized.bikeshare_trip_stations t ON t.station_id = p.end_station_id
            WHERE p.year = %s AND p.start_station_id = %s AND p.end_station_id <> %s
            ORDER BY p.trips DESC LIMIT %s""",
        (year, station_id, station_id, DESTINATIONS_SHOWN),
    ).fetchall()
    return {
        "station_id": station_id,
        "through": last,
        "totals": dict(totals),
        "weekday_hours": [dict(h) for h in hours],
        "destinations_year": year,
        "destinations": [dict(d) for d in destinations],
    }


def summary(conn) -> dict | None:
    """Trips by month, the busiest docks over the last 12 months, and the most popular
    routes in the latest year, for the report."""
    if (
        conn.execute("SELECT to_regclass('normalized.bikeshare_trip_files') AS t").fetchone()["t"]
        is None
    ):
        return None
    coverage = _coverage(conn)
    if coverage is None:
        return None
    last = coverage["last_trip"]
    months = conn.execute(
        """SELECT date_trunc('month', trip_date)::date AS month,
                  sum(departures)::int AS trips,
                  round(100.0 * sum(member_departures) / sum(departures), 1) AS member_pct,
                  round(100.0 * sum(ebike_departures) / sum(departures), 1) AS ebike_pct
           FROM normalized.bikeshare_trip_station_days GROUP BY 1 ORDER BY 1"""
    ).fetchall()
    docks = conn.execute(
        f"""WITH places AS ({DOCK_PLACES})
            SELECT d.station_id, coalesce(pl.name, t.name, d.station_id) AS name,
                   sum(d.departures + d.arrivals)::int AS trips
            FROM normalized.bikeshare_trip_station_days d
            LEFT JOIN places pl USING (station_id)
            LEFT JOIN normalized.bikeshare_trip_stations t USING (station_id)
            WHERE d.trip_date > %s - 365
            GROUP BY 1, 2 ORDER BY trips DESC LIMIT %s""",
        (last, DOCKS_SHOWN),
    ).fetchall()
    routes = conn.execute(
        f"""WITH places AS ({DOCK_PLACES})
            SELECT p.start_station_id, p.end_station_id,
                   coalesce(a.name, ta.name, p.start_station_id) AS start_name,
                   coalesce(b.name, tb.name, p.end_station_id) AS end_name,
                   p.trips, round(p.timed_seconds / nullif(p.timed_trips, 0) / 60.0)::int
                       AS typical_minutes
            FROM normalized.bikeshare_trip_pairs p
            LEFT JOIN places a ON a.station_id = p.start_station_id
            LEFT JOIN places b ON b.station_id = p.end_station_id
            LEFT JOIN normalized.bikeshare_trip_stations ta
              ON ta.station_id = p.start_station_id
            LEFT JOIN normalized.bikeshare_trip_stations tb ON tb.station_id = p.end_station_id
            WHERE p.year = %s AND p.start_station_id <> p.end_station_id
            ORDER BY p.trips DESC LIMIT %s""",
        (last.year, ROUTES_SHOWN),
    ).fetchall()
    files = conn.execute(
        """SELECT year, trips, end_name_copied, skipped FROM normalized.bikeshare_trip_files
           ORDER BY year"""
    ).fetchall()
    return {
        **coverage,
        "months": [dict(m) for m in months],
        "busiest_docks": [dict(d) for d in docks],
        "routes_year": last.year,
        "popular_routes": [dict(r) for r in routes],
        "files": [dict(f) for f in files],
    }
