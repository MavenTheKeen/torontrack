"""The Events page: the city's festivals and community events together with Ticketmaster's
concerts, games and shows, day by day, each with the nearest station to get there.

- Categories: concerts, sports, arts (theatre, comedy, film, exhibits) and community
  (festivals, markets, family and everything else), from the city's categories or
  Ticketmaster's segment and genre.
- An event at a major venue (arenas and stadiums) is flagged, since it fills the nearest
  station before it starts and when it ends.
- Exhibits, markets and attractions that run on most days would crowd every day, so an
  event on ONGOING_DAYS or more days of the window is marked ongoing; the page folds those
  away. For Ticketmaster, which has no series ID, that is one name at one venue.
- The city lists all-day events (installations, exhibits) at midnight; those are marked
  all_day and listed first.
- The nearest subway or regional station is found by straight-line distance and walking
  time at 80 m a minute, as in station panels; none is given beyond MAX_WALK_MIN.
"""

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TORONTO = ZoneInfo("America/Toronto")
DAYS = 14
ONGOING_DAYS = 5
WALK_M_PER_MIN = 80
MAX_WALK_MIN = 20
SUBWAY_PREFERENCE_MIN = 3
MAJOR_VENUES = (
    "scotiabank arena",
    "rogers centre",
    "bmo field",
    "budweiser stage",
    "coca-cola coliseum",
)
CATEGORIES = {
    "concerts": "Concerts",
    "sports": "Sports",
    "arts": "Arts & theatre",
    "community": "Festivals & community",
}
CITY_ARTS = {"Theatre", "Comedy", "Film", "Arts/Exhibits", "Museum", "Dance", "Literary"}


def city_category(categories: list[str]) -> str:
    cats = set(categories or [])
    if "Sports" in cats:
        return "sports"
    if "Music" in cats:
        return "concerts"
    if cats & CITY_ARTS:
        return "arts"
    return "community"


def ticketed_category(segment: str | None, genre: str | None) -> str:
    if segment == "Sports":
        return "sports"
    if segment == "Music":
        return "concerts"
    if segment in ("Arts & Theatre", "Film") or genre == "Comedy":
        return "arts"
    return "community"


def is_major(venue: str | None) -> bool:
    name = (venue or "").casefold()
    return any(v in name for v in MAJOR_VENUES)


def _metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(x, math.radians(lat2 - lat1)) * 6_371_000


def nearest_station(lat: float, lon: float, stations: list[dict]) -> dict | None:
    """The closest station within MAX_WALK_MIN; `stations` have name, lat, lon, kind and
    optionally key and lines. The subway, which runs every few minutes, is preferred unless
    a regional station is over SUBWAY_PREFERENCE_MIN closer."""
    best: dict[str, tuple[float, dict]] = {}
    for s in stations:
        m = _metres(lat, lon, s["lat"], s["lon"])
        if s["kind"] not in best or m < best[s["kind"]][0]:
            best[s["kind"]] = (m, s)
    walks = {kind: max(1, math.ceil(m / WALK_M_PER_MIN)) for kind, (m, _) in best.items()}
    if not walks:
        return None
    kind = min(walks, key=walks.get)
    if "subway" in walks and walks["subway"] <= walks[kind] + SUBWAY_PREFERENCE_MIN:
        kind = "subway"
    if walks[kind] > MAX_WALK_MIN:
        return None
    s = best[kind][1]
    return {
        "name": s["name"],
        "kind": s["kind"],
        "key": s.get("key"),
        "lines": s.get("lines", []),
        "walk_min": walks[kind],
    }


def window(now: datetime, days: int = DAYS) -> list[date]:
    today = now.astimezone(TORONTO).date()
    return [today + timedelta(days=i) for i in range(days)]


def city_rows(conn, first: date, last: date) -> list[dict]:
    start = datetime(first.year, first.month, first.day, tzinfo=TORONTO)
    end = datetime(last.year, last.month, last.day, tzinfo=TORONTO) + timedelta(days=1)
    return [
        dict(r)
        for r in conn.execute(
            """SELECT e.event_id, e.name, e.categories, e.featured, e.free, e.website,
                      e.venue, e.address, e.lat, e.lon, o.starts_at
               FROM normalized.city_events e
               JOIN normalized.city_event_occurrences o USING (event_id)
               WHERE o.starts_at >= %s AND o.starts_at < %s
               ORDER BY o.starts_at, e.name""",
            (start, end),
        )
    ]


def ticketed_rows(conn, first: date, last: date) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT event_id, name, segment, genre, starts_at, local_date, time_tbd,
                      status, venue, address, lat, lon, url, sessions
               FROM normalized.ticketed_events
               WHERE local_date BETWEEN %s AND %s AND status <> 'cancelled'
               ORDER BY local_date, starts_at NULLS LAST, name""",
            (first, last),
        )
    ]


def build(city: list[dict], ticketed: list[dict], stations: list[dict], days: list[date]) -> dict:
    """The agenda payload: the window's days and every event occurrence in it."""
    city_days: dict[str, set] = {}
    for r in city:
        city_days.setdefault(r["event_id"], set()).add(r["starts_at"].astimezone(TORONTO).date())
    # Ticketmaster has no series ID: one name at one venue on many days is one attraction.
    ticketed_days: dict[tuple, set] = {}
    for r in ticketed:
        ticketed_days.setdefault((r["name"].casefold(), r["venue"]), set()).add(r["local_date"])
    events = []
    near: dict[tuple, dict | None] = {}

    def station(lat, lon):
        key = (round(lat, 4), round(lon, 4))
        if key not in near:
            near[key] = nearest_station(lat, lon, stations)
        return near[key]

    for r in city:
        local = r["starts_at"].astimezone(TORONTO)
        events.append(
            {
                "id": f"city:{r['event_id']}:{int(r['starts_at'].timestamp())}",
                "source": "city",
                "name": r["name"],
                "category": city_category(r["categories"]),
                "date": local.date().isoformat(),
                "starts_at": r["starts_at"].isoformat(),
                "time_tbd": False,
                "all_day": local.hour == 0 and local.minute == 0,
                "venue": r["venue"],
                "address": r["address"],
                "lat": r["lat"],
                "lon": r["lon"],
                "url": r["website"],
                "free": r["free"],
                "featured": r["featured"],
                "major": is_major(r["venue"]),
                "status": None,
                "ongoing": len(city_days[r["event_id"]]) >= ONGOING_DAYS,
                "sessions": 1,
                "station": station(r["lat"], r["lon"]),
            }
        )
    for r in ticketed:
        events.append(
            {
                "id": f"tm:{r['event_id']}",
                "source": "ticketmaster",
                "name": r["name"],
                "category": ticketed_category(r["segment"], r["genre"]),
                "date": r["local_date"].isoformat(),
                "starts_at": r["starts_at"].isoformat() if r["starts_at"] else None,
                "time_tbd": r["time_tbd"],
                "all_day": False,
                "venue": r["venue"],
                "address": r["address"],
                "lat": r["lat"],
                "lon": r["lon"],
                "url": r["url"],
                "free": None,
                "featured": False,
                "major": is_major(r["venue"]),
                "status": r["status"] if r["status"] in ("postponed", "rescheduled") else None,
                "ongoing": len(ticketed_days[(r["name"].casefold(), r["venue"])]) >= ONGOING_DAYS,
                "sessions": r.get("sessions", 1),
                "station": station(r["lat"], r["lon"]),
            }
        )
    events.sort(
        key=lambda e: (e["date"], not e["all_day"], e["time_tbd"], e["starts_at"] or "", e["name"])
    )
    return {
        "days": [d.isoformat() for d in days],
        "categories": CATEGORIES,
        "events": events,
        "sources": {"ticketmaster": bool(ticketed)},
    }
