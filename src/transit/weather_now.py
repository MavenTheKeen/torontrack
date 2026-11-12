"""Current weather for the weather map: Environment and Climate Change Canada's city page
for Toronto (conditions, hourly and daily forecasts, warnings), the radar, and an hourly
wind and precipitation forecast grid.

Both come from MSC GeoMet. The city page is read from its OGC API; the radar from its WMS
as PNG images of the Toronto area, one for rain rate and one for snow rate, every six
minutes. Images are stored in the database and served by this site, so browsers never
contact a third party. Frames older than KEEP are dropped.

The forecast grid comes from the High Resolution Deterministic Prediction System (HRDPS)
through GeoMet's WCS, as small uncompressed GeoTIFFs of the same area: wind speed and
direction at 10 m, and accumulated precipitation, whose hour-to-hour difference is the
hourly amount. It is read again only when a newer model run is published.

Contains information licensed under the Open Government Licence – Canada.
"""

import json
import math
import re
import struct
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
RUN_DIMENSION = re.compile(r'<Dimension name="reference_time"[^>]*default="([^"]+)"')
GRID = {"speed": "HRDPS.CONTINENTAL_WSPD", "direction": "HRDPS.CONTINENTAL_WD",
        "precip": "HRDPS.CONTINENTAL_PR"}  # fmt: skip
GRID_HOURS = 13  # This hour and the next twelve.
GRID_CHECK = timedelta(minutes=30)
AREA = "toronto"


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


def read_tiff(data: bytes) -> tuple[int, int, list[float]]:
    """Width, height and values of a little-endian, uncompressed, single-band float32
    GeoTIFF, as GeoMet's WCS returns them; rows run north to south."""
    if data[:4] != b"II*\x00":
        raise FetchError("Forecast grid is not a little-endian TIFF")
    offset = struct.unpack_from("<I", data, 4)[0]
    tags = {}
    for i in range(struct.unpack_from("<H", data, offset)[0]):
        tag, kind, count, value = struct.unpack_from("<HHII", data, offset + 2 + 12 * i)
        if kind == 3 and count == 1:
            value = struct.unpack_from("<H", data, offset + 10 + 12 * i)[0]
        elif kind == 4 and count > 1:
            value = struct.unpack_from(f"<{count}I", data, value)
        elif kind == 3 and count > 1:
            value = struct.unpack_from(f"<{count}H", data, value)
        tags[tag] = value
    width, height = tags[256], tags[257]
    if tags.get(259, 1) != 1 or tags.get(258) != 32 or tags.get(339) != 3:
        raise FetchError("Forecast grid is not uncompressed float32")
    offsets = tags[273] if isinstance(tags[273], tuple) else (tags[273],)
    counts = tags[279] if isinstance(tags[279], tuple) else (tags[279],)
    raw = b"".join(data[o : o + c] for o, c in zip(offsets, counts, strict=True))
    if len(raw) != 4 * width * height:
        raise FetchError("Forecast grid has the wrong size")
    return width, height, list(struct.unpack(f"<{width * height}f", raw))


def grid_url(layer: str, at: datetime, run: datetime) -> str:
    west, south, east, north = BOUNDS
    stamp = "%Y-%m-%dT%H:%M:%SZ"
    query = [
        ("service", "WCS"), ("version", "2.0.1"), ("request", "GetCoverage"),
        ("coverageId", layer), ("subsettingCrs", "EPSG:4326"), ("subset", f"x({west},{east})"),
        ("subset", f"y({south},{north})"), ("outputCrs", "EPSG:4326"),
        ("format", "image/tiff"), ("time", at.strftime(stamp)),
        ("dim_reference_time", run.strftime(stamp)),
    ]  # fmt: skip
    return f"https://{WMS_HOST}/geomet?{urlencode(query)}"


def wind_vector(speed: float, direction: float) -> tuple[float, float]:
    """Eastward and northward components in m/s of a wind blowing from `direction` degrees."""
    rad = math.radians(direction)
    return -speed * math.sin(rad), -speed * math.cos(rad)


def forecast_hours(now: datetime, run: datetime) -> list[datetime]:
    """This hour and the next ones the run covers, from its first forecast hour."""
    first = max(now.replace(minute=0, second=0, microsecond=0), run + timedelta(hours=1))
    last = run + timedelta(hours=48)
    hours = [first + timedelta(hours=i) for i in range(GRID_HOURS)]
    return [h for h in hours if h <= last]


def read_grid(client: FeedClient, run: datetime, now: datetime) -> dict:
    """The run's wind (u, v in m/s) and hourly precipitation (mm) for the coming hours."""

    def layer(name: str, at: datetime) -> tuple[int, int, list[float]]:
        return read_tiff(client.fetch_bytes(grid_url(GRID[name], at, run), host=WMS_HOST))

    hours = []
    previous = None
    width = height = None
    for at in forecast_hours(now, run):
        width, height, speed = layer("speed", at)
        _, _, direction = layer("direction", at)
        _, _, total = layer("precip", at)
        if previous is None:
            before = at - timedelta(hours=1)
            previous = [0.0] * len(total) if before <= run else layer("precip", before)[2]
        u, v = zip(*(wind_vector(s, d) for s, d in zip(speed, direction, strict=True)),
                   strict=True)  # fmt: skip
        hours.append({
            "at": at.isoformat(),
            "u": [round(x, 1) for x in u],
            "v": [round(x, 1) for x in v],
            "precip": [round(max(t - p, 0.0), 1) for t, p in zip(total, previous, strict=True)],
        })  # fmt: skip
        previous = total
    if not hours:
        raise FetchError("The model run covers no coming hours")
    west, south, east, north = BOUNDS
    return {
        "run": run.isoformat(), "west": west, "north": north, "width": width, "height": height,
        "dx": (east - west) / width, "dy": (north - south) / height, "hours": hours,
    }  # fmt: skip


def refresh_grid(conn, client: FeedClient, started: datetime) -> int:
    """Reads the newest model run when it is new; the number of hours stored."""
    row = conn.execute(
        "SELECT run_at, fetched_at FROM normalized.weather_grid WHERE area = %s", (AREA,)
    ).fetchone()
    if row and row["fetched_at"] > started - GRID_CHECK:
        return 0
    capabilities = client.fetch_bytes(
        f"https://{WMS_HOST}/geomet?"
        + urlencode(
            {
                "service": "WMS",
                "version": "1.3.0",
                "request": "GetCapabilities",
                "layer": GRID["precip"],
            }
        ),  # fmt: skip
        host=WMS_HOST,
    ).decode("utf-8", "replace")
    match = RUN_DIMENSION.search(capabilities)
    if not match:
        raise FetchError("Forecast capabilities have no model run")
    run = datetime.fromisoformat(match[1].replace("Z", "+00:00"))
    if row and row["run_at"] >= run:
        conn.execute(
            "UPDATE normalized.weather_grid SET fetched_at = %s WHERE area = %s",
            (started, AREA),
        )
        return 0
    grid = read_grid(client, run, started)
    conn.execute(
        """INSERT INTO normalized.weather_grid (area, run_at, fetched_at, grid)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (area) DO UPDATE SET run_at = EXCLUDED.run_at,
             fetched_at = EXCLUDED.fetched_at, grid = EXCLUDED.grid""",
        (AREA, run, started, json.dumps(grid)),
    )
    return len(grid["hours"])


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
        "wind_dir": _value(((now.get("wind") or {}).get("direction") or {}).get("value")),
        "gust_kmh": _value(((now.get("wind") or {}).get("gust") or {}).get("value")),
        "humidity": _value((now.get("relativeHumidity") or {}).get("value")),
        "hourly": [
            {
                "at": h.get("timestamp"),
                "temperature": _value((h.get("temperature") or {}).get("value")),
                "condition": _value(h.get("condition")),
                "precip_chance": _value((h.get("lop") or {}).get("value")),
                "wind_kmh": _value(((h.get("wind") or {}).get("speed") or {}).get("value")),
                "wind_dir": _value(((h.get("wind") or {}).get("direction") or {}).get("value")),
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
        # The forecast grid is separate: without it the map still has the radar.
        try:
            result["grid_hours"] = refresh_grid(conn, client, started)
        except (FetchError, KeyError, IndexError, ValueError, TypeError, struct.error) as exc:
            result["error"] = (result.get("error", "") + f" Forecast grid: {exc}")[:1000].strip()
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
    grid = conn.execute(
        "SELECT grid FROM normalized.weather_grid WHERE area = %s", (AREA,)
    ).fetchone()
    if page is None and not any(frames.values()) and grid is None:
        return None
    west, south, east, north = BOUNDS
    return {
        "page": page["summary"] if page else None,
        "fetched_at": page["fetched_at"] if page else None,
        "grid": grid["grid"] if grid else None,
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
