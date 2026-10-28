"""Input files for the trip planner (OpenTripPlanner), and the configuration it builds from.

The planner routes on these public sources, downloaded here into one directory that the
OpenTripPlanner container reads:

- the TTC schedule (GTFS, City of Toronto Open Data, Open Government Licence – Toronto);
- the GO Transit and UP Express schedule (GTFS, Metrolinx Open Data);
- the Brampton Transit, MiWay and York Region Transit schedules (transit.agencies);
- streets and paths for walking, from OpenStreetMap (© OpenStreetMap contributors, ODbL),
  as OpenStreetMap France's daily Golden Horseshoe extract. Trips must start and end
  inside EXTENT, the area the map shows buses in.

Files are replaced only when their content changes, and manifest.json is rewritten only
then, so the build step can rebuild the graph only when the manifest is newer than it.
"""

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from transit import agencies, maptiles, regional
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_GTFS_URL, STATIC_HOST

OSM_HOST = "download.openstreetmap.fr"
OSM_URL = f"https://{OSM_HOST}/extracts/north-america/canada/ontario/golden_horseshoe.osm.pbf"
OSM_FILE = "golden-horseshoe.osm.pbf"
# West, south, east, north: Brampton, Mississauga and York Region up to Keswick, well
# inside the extract (longitude -80.27 to -78.41, latitude 42.67 to 44.54).
EXTENT = maptiles.BOUNDS
SOURCES = {
    "ttc-gtfs.zip": (STATIC_GTFS_URL, STATIC_HOST, 200 << 20),
    "go-gtfs.zip": (regional.GTFS_URL, regional.HOST, 100 << 20),
    **{
        f"{a.key}-gtfs.zip": (a.static_url, a.host(a.static_url), agencies.STATIC_MAX_BYTES)
        for a in agencies.AGENCIES.values()
    },
    OSM_FILE: (OSM_URL, OSM_HOST, 400 << 20),
}
# Inputs of earlier versions, removed so the build does not read them.
OLD_FILES = ("toronto.osm.pbf",)
MANIFEST = "manifest.json"
# GBFS version 2 discovery; the collector reads version 3 of the same feed.
BIKE_SHARE_GBFS = "https://toronto.publicbikesystem.net/customer/gbfs/v2/gbfs.json"

BUILD_CONFIG = {
    "transitFeeds": [
        {"type": "gtfs", "feedId": "ttc", "source": "ttc-gtfs.zip"},
        {"type": "gtfs", "feedId": "go", "source": "go-gtfs.zip"},
        *(
            {"type": "gtfs", "feedId": key, "source": f"{key}-gtfs.zip"}
            for key in agencies.AGENCIES
        ),
    ],
    "osm": [{"source": OSM_FILE, "timeZone": "America/Toronto"}],
    # MiWay names America/New_York, the same clock; OpenTripPlanner needs one zone.
    "transitModelTimeZone": "America/Toronto",
    # Keep a week of past service for late-night trips and two months ahead.
    "transitServiceStart": "-P7D",
    "transitServiceEnd": "P60D",
}


def _trip_updates(feed_id: str, url: str) -> dict:
    return {
        "type": "stop-time-updater",
        "feedId": feed_id,
        "url": url,
        "frequency": "1m",
        "backwardsDelayPropagationType": "REQUIRED_NO_DATA",
    }


ROUTER_CONFIG = {
    "server": {"apiProcessingTimeout": "8s"},
    # Bike Share stations and their bikes and docks, read by the planner every minute, for
    # trips that may use Bike Share (off unless the visitor switches it on).
    "updaters": [
        {
            "type": "vehicle-rental",
            "sourceType": "gbfs",
            "url": BIKE_SHARE_GBFS,
            "frequency": "1m",
            "network": "bike_share_toronto",
        },
        # Live trip updates from the three neighbouring agencies, so their buses are planned
        # at the times they are expected. Not the TTC's: its live trip IDs are not its
        # timetable's, so none match. Not Metrolinx's: its URL carries the API key, and
        # OpenTripPlanner logs each updater's URL.
        *(_trip_updates(key, a.trips_url) for key, a in agencies.AGENCIES.items()),
    ],
}
# OpenTripPlanner turns this self-check on by default and logs that production should not.
OTP_CONFIG = {"otpFeatures": {"CostlyAssertions": False}}


def _write_json(path: Path, value: dict) -> bool:
    text = json.dumps(value, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def prepare(out: str | os.PathLike, client: FeedClient) -> dict:
    """Download the sources into `out`; {"changed": bool, "files": {name: sha256}}."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / MANIFEST
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))["files"]
    except (OSError, ValueError, KeyError):
        previous = {}
    files, changed = {}, False
    for name, (url, host, max_bytes) in SOURCES.items():
        with tempfile.NamedTemporaryFile(dir=out, prefix=f".{name}.", delete=False) as tmp:
            try:
                client.fetch_bytes(url, host=host, max_bytes=max_bytes, sink=tmp)
                tmp.seek(0)
                digest = hashlib.file_digest(tmp, "sha256").hexdigest()
                size = tmp.tell()
            except BaseException:
                tmp.close()
                os.unlink(tmp.name)
                raise
        if size == 0:
            os.unlink(tmp.name)
            raise FetchError(f"{name} download is empty")
        if digest == previous.get(name) and (out / name).exists():
            os.unlink(tmp.name)
        else:
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, out / name)
            changed = True
        files[name] = digest
    for name in OLD_FILES:
        if (out / name).exists():
            (out / name).unlink()
            changed = True
    changed |= _write_json(out / "build-config.json", BUILD_CONFIG)
    changed |= _write_json(out / "router-config.json", ROUTER_CONFIG)
    changed |= _write_json(out / "otp-config.json", OTP_CONFIG)
    if changed or not manifest_path.exists():
        _write_json(manifest_path, {"prepared_at": datetime.now(UTC).isoformat(), "files": files})
    return {"changed": changed, "files": files}
