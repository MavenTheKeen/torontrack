"""Hourly weather from Environment and Climate Change Canada's climate-hourly collection.

Unlike the realtime transit feeds, ECCC keeps history, so a missed refresh is caught up
by the next one and the whole collection period can be backfilled. Observations appear
several hours late and can be revised after quality control, so each refresh re-reads
the last REFETCH window and overwrites those hours.
"""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from psycopg.types.json import Jsonb

from transit.db import connect
from transit.http import FeedClient, FetchError
from transit.ttc.realtime import TORONTO

HOST = "api.weather.gc.ca"
# Toronto City (University of Toronto, downtown): central to the subway and Bike Share.
CLIMATE_ID = "6158355"
REFETCH = timedelta(days=2)
RAW_KEEP = timedelta(days=7)
MAX_HOURS = 10_000
FIELDS = {
    "TEMP": "temp_c",
    "DEW_POINT_TEMP": "dew_point_c",
    "RELATIVE_HUMIDITY": "relative_humidity",
    "PRECIP_AMOUNT": "precip_mm",
    "STATION_PRESSURE": "station_pressure_kpa",
    "WIND_SPEED": "wind_speed_kmh",
    "WIND_DIRECTION": "wind_direction_10deg",
    "VISIBILITY": "visibility_km",
    "HUMIDEX": "humidex",
    "WINDCHILL": "windchill",
}


def url(start: datetime, end: datetime, climate_id: str = CLIMATE_ID) -> str:
    """The API filters on LOCAL_DATE, the station's local standard time (no daylight
    saving), written with a Z. Using Toronto clock time shifts the window by at most an
    hour, which the REFETCH margin absorbs; stored times come from UTC_DATE."""
    local = "%Y-%m-%dT%H:%M:%SZ"
    query = {
        "CLIMATE_IDENTIFIER": climate_id,
        "datetime": f"{start.astimezone(TORONTO):{local}}/{end.astimezone(TORONTO):{local}}",
        "sortby": "LOCAL_DATE",
        "limit": MAX_HOURS,
        "f": "json",
    }
    return f"https://{HOST}/collections/climate-hourly/items?{urlencode(query)}"


def parse(payload: dict) -> list[dict]:
    features = payload.get("features")
    if not isinstance(features, list):
        raise FetchError("Weather response has no features list")
    matched = payload.get("numberMatched")
    if isinstance(matched, int) and matched > len(features):
        raise FetchError(
            f"Weather response holds {len(features)} of {matched} hours; backfill a shorter range"
        )
    rows = []
    for feature in features:
        p = feature.get("properties") or {}
        try:
            observed_at = datetime.fromisoformat(p["UTC_DATE"]).replace(tzinfo=UTC)
            row = {
                "climate_id": str(p["CLIMATE_IDENTIFIER"]),
                "observed_at": observed_at,
                "station_name": str(p["STATION_NAME"]),
            }
        except (KeyError, TypeError, ValueError):
            continue  # Without a station and time the hour cannot be placed.
        for source, column in FIELDS.items():
            row[column] = p.get(source)
        weather = p.get("WEATHER_ENG_DESC")
        row["weather"] = None if weather in (None, "", "NA") else weather
        row["flags"] = {source: p[f"{source}_FLAG"] for source in FIELDS if p.get(f"{source}_FLAG")}
        rows.append(row)
    return rows


def load(conn, payload: dict, source_url: str, fetched_at: datetime) -> list[dict]:
    rows = parse(payload)
    response_id = conn.execute(
        """INSERT INTO raw.weather_responses (fetched_at, url, payload)
           VALUES (%s, %s, %s) RETURNING response_id""",
        (fetched_at, source_url, Jsonb(payload)),
    ).fetchone()["response_id"]
    columns = ["climate_id", "observed_at", "station_name", *FIELDS.values(), "weather"]
    for row in rows:
        conn.execute(
            f"""INSERT INTO normalized.weather_hourly ({", ".join(columns)}, flags, response_id)
                VALUES ({", ".join(f"%({c})s" for c in columns)}, %(flags)s, %(response_id)s)
                ON CONFLICT (climate_id, observed_at) DO UPDATE SET
                {", ".join(f"{c} = EXCLUDED.{c}" for c in columns[2:])},
                flags = EXCLUDED.flags, response_id = EXCLUDED.response_id""",
            {**row, "flags": Jsonb(row["flags"]), "response_id": response_id},
        )
    # Responses no hour points to any more were superseded by later fetches.
    conn.execute(
        """DELETE FROM raw.weather_responses r WHERE fetched_at < %s
           AND NOT EXISTS (SELECT 1 FROM normalized.weather_hourly w
                           WHERE w.response_id = r.response_id)""",
        (fetched_at - RAW_KEEP,),
    )
    return rows


def refresh(database_url: str, client: FeedClient, now: datetime, since=None) -> dict:
    """Load hours from `since` (default: REFETCH before the latest stored hour)."""
    started = datetime.now(UTC)
    with connect(database_url) as conn:
        try:
            if since is None:
                latest = conn.execute(
                    "SELECT max(observed_at) AS at FROM normalized.weather_hourly"
                    " WHERE climate_id = %s",
                    (CLIMATE_ID,),
                ).fetchone()["at"]
                since = (latest or now) - REFETCH
            source = url(since, now)
            try:
                payload = json.loads(client.fetch_bytes(source, host=HOST))
            except ValueError as exc:
                raise FetchError("Weather response is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise FetchError("Weather response root must be an object")
            rows = load(conn, payload, source, started)
            latest = max((r["observed_at"] for r in rows), default=None)
            result = {"status": "loaded", "hours": len(rows), "latest_observation": latest}
        except FetchError as exc:
            result = {"status": "failed", "hours": None, "latest_observation": None}
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.weather_refreshes
               (started_at, finished_at, status, hours, latest_observation, error)
               VALUES (%s, now(), %s, %s, %s, %s)""",
            (
                started,
                result["status"],
                result["hours"],
                result["latest_observation"],
                result.get("error"),
            ),
        )
    return result
