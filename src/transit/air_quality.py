"""Air Quality Health Index (AQHI) for Toronto from Environment and Climate Change Canada.

ECCC's MSC GeoMet API publishes hourly AQHI observations and forecasts for its Toronto
stations (Downtown, East, North, West and a Toronto-wide value). Each hourly run keeps the
observations of the last two days, which grow into a history, and replaces each station's
forecast with its newest publication.

Contains information licensed under the Open Government Licence – Canada.
"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from transit.db import connect
from transit.http import FeedClient, FetchError

HOST = "api.weather.gc.ca"
BBOX = "-79.65,43.58,-79.10,43.86"
LOOKBACK = timedelta(days=2)
MAIN = "FCWYG"  # Toronto Downtown, shown first.
# ECCC's health risk categories by AQHI value.
RISKS = ((3, "Low"), (6, "Moderate"), (10, "High"), (float("inf"), "Very high"))


def risk(value: float) -> str:
    rounded = round(value)
    return next(label for top, label in RISKS if rounded <= top)


def _items(client: FeedClient, collection: str, **query) -> list[dict]:
    url = f"https://{HOST}/collections/{collection}/items?" + urlencode(
        {"f": "json", "bbox": BBOX, "limit": 2000, **query}
    )
    payload = json.loads(client.fetch_bytes(url, host=HOST, max_bytes=20 << 20))
    return [f["properties"] for f in payload.get("features", [])]


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def read_observations(items: list[dict]) -> list[tuple]:
    return [
        (i["location_id"], i["location_name_en"], _time(i["observation_datetime"]), i["aqhi"])
        for i in items
        if i.get("aqhi") is not None and i.get("location_id")
    ]


def read_forecasts(items: list[dict]) -> list[tuple]:
    """Each station's newest publication only."""
    newest = {}
    for i in items:
        if i.get("aqhi") is None:
            continue
        published = _time(i["publication_datetime"])
        if published > newest.get(i["location_id"], datetime.min.replace(tzinfo=UTC)):
            newest[i["location_id"]] = published
    return [
        (i["location_id"], _time(i["publication_datetime"]), _time(i["forecast_datetime"]),
         i["aqhi"])
        for i in items
        if i.get("aqhi") is not None and _time(i["publication_datetime"]) == newest.get(
            i["location_id"])
    ]  # fmt: skip


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed"}
    with connect(database_url) as conn:
        try:
            since = (started - LOOKBACK).strftime("%Y-%m-%dT%H:%M:%SZ")
            observations = read_observations(
                _items(client, "aqhi-observations-realtime", datetime=f"{since}/..")
            )
            forecasts = read_forecasts(_items(client, "aqhi-forecasts-realtime"))
            if not observations:
                raise FetchError("No AQHI observations for Toronto")
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO normalized.aqhi_observations (location_id, location_name,
                           observed_at, aqhi) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (location_id, observed_at) DO UPDATE SET
                         aqhi = EXCLUDED.aqhi, location_name = EXCLUDED.location_name""",
                    observations,
                )
                for location in {f[0] for f in forecasts}:
                    cur.execute(
                        "DELETE FROM normalized.aqhi_forecasts WHERE location_id = %s",
                        (location,),
                    )
                cur.executemany(
                    """INSERT INTO normalized.aqhi_forecasts (location_id, published_at,
                           forecast_at, aqhi) VALUES (%s, %s, %s, %s)""",
                    forecasts,
                )
            result.update(status="loaded", observations=len(observations),
                          forecasts=len(forecasts))  # fmt: skip
        except (FetchError, KeyError, ValueError, TypeError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.aqhi_refreshes (started_at, finished_at, status, observations,
                   forecasts, error) VALUES (%s, now(), %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result.get("observations"),
                result.get("forecasts"),
                result.get("error"),
            ),
        )
    return result


def current(conn, now: datetime) -> dict | None:
    """Each station's latest observation within six hours, Downtown first, and Downtown's
    forecast from now on, with ECCC's risk category."""
    stations = conn.execute(
        """SELECT DISTINCT ON (location_id) location_id, location_name, observed_at, aqhi
           FROM normalized.aqhi_observations WHERE observed_at > %s
           ORDER BY location_id, observed_at DESC""",
        (now - timedelta(hours=6),),
    ).fetchall()
    if not stations:
        return None
    stations.sort(key=lambda s: (s["location_id"] != MAIN, s["location_name"]))
    forecast = conn.execute(
        """SELECT forecast_at, aqhi FROM normalized.aqhi_forecasts
           WHERE location_id = %s AND forecast_at >= %s ORDER BY forecast_at""",
        (MAIN, now - timedelta(hours=1)),
    ).fetchall()
    return {
        "stations": [
            {"id": s["location_id"], "name": s["location_name"], "observed_at": s["observed_at"],
             "aqhi": float(s["aqhi"]), "risk": risk(float(s["aqhi"]))}
            for s in stations
        ],
        "forecast": [
            {"at": f["forecast_at"], "aqhi": float(f["aqhi"]), "risk": risk(float(f["aqhi"]))}
            for f in forecast
        ],
    }  # fmt: skip
