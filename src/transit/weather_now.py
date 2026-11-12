"""Current weather for the weather map: Environment and Climate Change Canada's city page
for Toronto (conditions, hourly and daily forecasts, warnings) and the radar.

Both come from MSC GeoMet. The city page is read from its OGC API; the radar from its WMS
as PNG images of the Toronto area, one for rain rate and one for snow rate, every six
minutes. Images are stored in the database and served by this site, so browsers never
contact a third party. Frames older than KEEP are dropped.

Contains information licensed under the Open Government Licence – Canada.
"""

import json
import math
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from transit.db import connect
from transit.http import FeedClient, FetchError

API_HOST = "api.weather.gc.ca"
WMS_HOST = "geo.weather.gc.ca"
CITY = "on-143"  # Toronto.
LAYERS = {"rain": "RADAR_1KM_RRAI", "snow": "RADAR_1KM_RSNO"}
# The radar image covers the Toronto area: west, south, east, north in degrees.
BOUNDS = (-80.1, 43.25, -78.7, 44.15)
WIDTH = 800
FRAMES = 10  # One hour at six-minute steps.
KEEP = timedelta(hours=2)
TIME_DIMENSION = re.compile(r'<Dimension name="time"[^>]*>([^<]+)</Dimension>')


def _mercator(lon: float, lat: float) -> tuple[float, float]:
    x = math.radians(lon) * 6378137
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * 6378137
    return x, y


def image_size() -> tuple[int, int]:
    west, south, east, north = BOUNDS
    x1, y1 = _mercator(west, south)
    x2, y2 = _mercator(east, north)
    return WIDTH, round(WIDTH * (y2 - y1) / (x2 - x1))


def frame_url(layer: str, at: datetime) -> str:
    west, south, east, north = BOUNDS
    x1, y1 = _mercator(west, south)
    x2, y2 = _mercator(east, north)
    width, height = image_size()
    query = {
        "service": "WMS", "version": "1.3.0", "request": "GetMap", "layers": layer,
        "styles": "", "crs": "EPSG:3857", "bbox": f"{x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f}",
        "width": width, "height": height, "format": "image/png", "transparent": "true",
        "time": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }  # fmt: skip
    return f"https://{WMS_HOST}/geomet?{urlencode(query)}"


def latest_times(capabilities: str, count: int = FRAMES) -> list[datetime]:
    """The last `count` times of a layer's time dimension, start/end/period, oldest first."""
    match = TIME_DIMENSION.search(capabilities)
    if not match:
        raise FetchError("Radar capabilities have no time dimension")
    start, end, period = match[1].strip().split("/")
    step = int(re.fullmatch(r"PT(\d+)M", period)[1])
    start, end = (datetime.fromisoformat(t.replace("Z", "+00:00")) for t in (start, end))
    times = []
    at = end
    while at >= start and len(times) < count:
        times.append(at)
        at -= timedelta(minutes=step)
    return times[::-1]


def _value(field):
    """The English value of a city page field such as {"value": {"en": 7, "fr": 7}}."""
    if isinstance(field, dict):
        if "en" in field:
            return field["en"]
        if "value" in field:
            return _value(field["value"])
    return field


def summarize(page: dict) -> dict:
    """The parts of the city page the map shows, in plain values."""
    now = page.get("currentConditions") or {}
    hourly = (page.get("hourlyForecastGroup") or {}).get("hourlyForecasts") or []
    daily = (page.get("forecastGroup") or {}).get("forecasts") or []
    feels = _value((now.get("windChill") or {}).get("value")) or _value(
        (now.get("humidex") or {}).get("value")
    )
    return {
        "updated": page.get("lastUpdated"),
        "observed_at": _value(now.get("timestamp")),
        "condition": _value(now.get("condition")),
        "temperature": _value((now.get("temperature") or {}).get("value")),
        "feels_like": feels,
        "wind_kmh": _value(((now.get("wind") or {}).get("speed") or {}).get("value")),
        "humidity": _value((now.get("relativeHumidity") or {}).get("value")),
        "hourly": [
            {
                "at": h.get("timestamp"),
                "temperature": _value((h.get("temperature") or {}).get("value")),
                "condition": _value(h.get("condition")),
                "precip_chance": _value((h.get("lop") or {}).get("value")),
            }
            for h in hourly[:24]
        ],
        "daily": [
            {
                "name": _value((d.get("period") or {}).get("textForecastName")),
                "summary": _value(d.get("textSummary")),
                "temperature": _value(
                    ((d.get("temperatures") or {}).get("temperature") or [{}])[0].get("value")
                ),
                "precip": [
                    _value(p.get("value"))
                    for p in (d.get("precipitation") or {}).get("precipPeriods") or []
                ],
            }
            for d in daily[:6]
        ],  # fmt: skip
        "warnings": [
            {
                "type": _value(w.get("type")),
                "description": _value(w.get("description")),
                "url": _value(w.get("url")),
            }
            for w in page.get("warnings") or []
        ],
    }


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed", "frames": 0}
    with connect(database_url) as conn:
        try:
            page = json.loads(
                client.fetch_bytes(
                    f"https://{API_HOST}/collections/citypageweather-realtime/items?"
                    + urlencode({"f": "json", "identifier": CITY, "limit": 1}),
                    host=API_HOST,
                )
            )["features"][0]["properties"]
            conn.execute(
                """INSERT INTO normalized.weather_citypage (city, fetched_at, summary)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (city) DO UPDATE SET fetched_at = EXCLUDED.fetched_at,
                     summary = EXCLUDED.summary""",
                (CITY, started, json.dumps(summarize(page))),
            )
            have = {
                (r["kind"], r["frame_at"])
                for r in conn.execute("SELECT kind, frame_at FROM normalized.radar_frames")
            }
            for kind, layer in LAYERS.items():
                capabilities = client.fetch_bytes(
                    f"https://{WMS_HOST}/geomet?"
                    + urlencode(
                        {
                            "service": "WMS",
                            "version": "1.3.0",
                            "request": "GetCapabilities",
                            "layer": layer,
                        }
                    ),  # fmt: skip
                    host=WMS_HOST,
                ).decode("utf-8", "replace")
                for at in latest_times(capabilities):
                    if (kind, at) in have:
                        continue
                    png = client.fetch_bytes(frame_url(layer, at), host=WMS_HOST)
                    if not png.startswith(b"\x89PNG"):
                        raise FetchError(f"Radar frame {kind} {at} is not a PNG")
                    conn.execute(
                        """INSERT INTO normalized.radar_frames (kind, frame_at, png)
                           VALUES (%s, %s, %s) ON CONFLICT DO NOTHING""",
                        (kind, at, png),
                    )
                    result["frames"] += 1
            conn.execute(
                "DELETE FROM normalized.radar_frames WHERE frame_at < %s", (started - KEEP,)
            )
            result["status"] = "loaded"
        except (FetchError, KeyError, IndexError, ValueError, TypeError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.weather_now_refreshes (started_at, finished_at, status, frames,
                   error) VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], result["frames"], result.get("error")),
        )
    return result


def current(conn) -> dict | None:
    """The stored city page summary and the radar frames available, newest last."""
    page = conn.execute(
        "SELECT fetched_at, summary FROM normalized.weather_citypage WHERE city = %s", (CITY,)
    ).fetchone()
    frames = {kind: [] for kind in LAYERS}
    for r in conn.execute(
        """SELECT kind, frame_at FROM (
               SELECT kind, frame_at,
                      row_number() OVER (PARTITION BY kind ORDER BY frame_at DESC) AS n
               FROM normalized.radar_frames) x
           WHERE n <= %s ORDER BY frame_at""",
        (FRAMES,),
    ):
        frames[r["kind"]].append(r["frame_at"])
    if page is None and not any(frames.values()):
        return None
    west, south, east, north = BOUNDS
    return {
        "page": page["summary"] if page else None,
        "fetched_at": page["fetched_at"] if page else None,
        "radar": {
            "bounds": [[west, north], [east, north], [east, south], [west, south]],
            "frames": frames,
        },
    }


def frame(conn, kind: str, at: datetime) -> bytes | None:
    row = conn.execute(
        "SELECT png FROM normalized.radar_frames WHERE kind = %s AND frame_at = %s", (kind, at)
    ).fetchone()
    return bytes(row["png"]) if row else None
