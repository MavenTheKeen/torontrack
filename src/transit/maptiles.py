"""Build the site's own street map: a Toronto extract of Protomaps' daily OpenStreetMap
basemap, as one PMTiles file the web app serves itself.

Visitors' browsers then fetch map tiles only from this site, keeping the promise of no
third-party requests. The planet build is ~140 GB, so only the directory entries and the
tiles inside BOUNDS are read, with HTTP range requests; neighbouring tiles are fetched
together. The file is written under a temporary name and renamed into place, with a JSON
sidecar recording the source build, tile count and SHA-256. Beyond Toronto, the regional
rail network gets streets at regional detail only (REGION up to REGION_MAX_ZOOM).
"""

import gzip
import hashlib
import json
import math
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pmtiles.tile import (
    Compression,
    deserialize_directory,
    deserialize_header,
    find_tile,
    zxy_to_tileid,
)
from pmtiles.writer import Writer

from transit.http import FetchError

BUILDS_URL = "https://build-metadata.protomaps.dev/builds.json"
BUILD_HOST = "build.protomaps.com"
# West, south, east, north: every subway station and Bike Share dock with a margin, and
# Brampton and Mississauga, whose buses the map shows (transit.agencies).
BOUNDS = (-79.98, 43.47, -79.05, 43.92)
MAX_ZOOM = 15  # The map overzooms past this; z15 has every street and building.
# Every regional rail station (Stratford to Bowmanville, Niagara Falls to Barrie).
REGION = (-81.2, 42.95, -78.45, 44.55)
REGION_MAX_ZOOM = 12
FILE_NAME = "toronto.pmtiles"
ATTRIBUTION = "© OpenStreetMap contributors; basemap by Protomaps"
# Fetch neighbouring tiles in one request when the bytes between them are this small.
MERGE_GAP = 256 * 1024
MAX_SPAN = 16 * 1024 * 1024


def tiles_in_bounds(bounds=BOUNDS, max_zoom=MAX_ZOOM) -> list[int]:
    west, south, east, north = bounds

    def tile_x(lon, z):
        return int((lon + 180) / 360 * 2**z)

    def tile_y(lat, z):
        rad = math.radians(lat)
        return int((1 - math.asinh(math.tan(rad)) / math.pi) / 2 * 2**z)

    ids = []
    for z in range(max_zoom + 1):
        for x in range(tile_x(west, z), tile_x(east, z) + 1):
            for y in range(tile_y(north, z), tile_y(south, z) + 1):
                ids.append(zxy_to_tileid(z, x, y))
    return sorted(ids)


class Source:
    """Range reads from one planet build; only BUILD_HOST over HTTPS."""

    def __init__(self, url: str, client: httpx.Client):
        if not url.startswith(f"https://{BUILD_HOST}/"):
            raise FetchError("Map builds must come from the Protomaps build host")
        self.url, self.client, self.requests = url, client, 0

    def read(self, offset: int, length: int) -> bytes:
        self.requests += 1
        response = self.client.get(
            self.url, headers={"Range": f"bytes={offset}-{offset + length - 1}"}
        )
        if response.status_code != 206 or len(response.content) != length:
            raise FetchError(f"Map build range read failed: HTTP {response.status_code}")
        return response.content


def latest_build(client: httpx.Client) -> str:
    response = client.get(BUILDS_URL)
    response.raise_for_status()
    builds = [b for b in response.json() if str(b.get("key", "")).endswith(".pmtiles")]
    if not builds:
        raise FetchError("No Protomaps builds listed")
    return max(builds, key=lambda b: b["uploaded"])["key"]


def locate(source: Source, header: dict, tile_ids: list[int]) -> list[tuple[int, int, int]]:
    """(tile_id, offset in tile data, length) for every tile present, via the directories.
    Leaf directories are cached: neighbouring tiles share them."""
    root = deserialize_directory(source.read(header["root_offset"], header["root_length"]))
    leaves: dict[int, list] = {}
    found = []
    for tile_id in tile_ids:
        directory = root
        for _ in range(4):  # PMTiles directories are at most four levels deep.
            entry = find_tile(directory, tile_id)
            if entry is None:
                break
            if entry.run_length > 0:
                found.append((tile_id, entry.offset, entry.length))
                break
            if entry.offset not in leaves:
                leaves[entry.offset] = deserialize_directory(
                    source.read(header["leaf_directory_offset"] + entry.offset, entry.length)
                )
            directory = leaves[entry.offset]
    return found


def fetch_tiles(source: Source, header: dict, located) -> dict[int, bytes]:
    """Read tile bytes, merging nearby ranges into single requests."""
    by_offset = sorted(located, key=lambda t: t[1])
    tiles, i = {}, 0
    while i < len(by_offset):
        start = by_offset[i][1]
        end = start + by_offset[i][2]
        j = i + 1
        while (
            j < len(by_offset)
            and by_offset[j][1] - end <= MERGE_GAP
            and by_offset[j][1] + by_offset[j][2] - start <= MAX_SPAN
        ):
            end = max(end, by_offset[j][1] + by_offset[j][2])
            j += 1
        span = source.read(header["tile_data_offset"] + start, end - start)
        for tile_id, offset, length in by_offset[i:j]:
            tiles[tile_id] = span[offset - start : offset - start + length]
        i = j
    return tiles


def build(out_dir: str, client: httpx.Client | None = None, build_key: str | None = None) -> dict:
    own_client = client is None
    client = client or httpx.Client(
        timeout=httpx.Timeout(60.0, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "Torontrack/0.1 (portfolio project)"},
    )
    try:
        build_key = build_key or latest_build(client)
        source = Source(f"https://{BUILD_HOST}/{build_key}", client)
        header = deserialize_header(source.read(0, 127))
        if header["tile_compression"] not in (Compression.GZIP, Compression.NONE):
            raise FetchError("Unexpected tile compression in map build")
        metadata = source.read(header["metadata_offset"], header["metadata_length"])
        if header["internal_compression"] == Compression.GZIP:
            metadata = gzip.decompress(metadata)
        metadata = json.loads(metadata)
        wanted = set(tiles_in_bounds()) | set(tiles_in_bounds(REGION, REGION_MAX_ZOOM))
        located = locate(source, header, sorted(wanted))
        tiles = fetch_tiles(source, header, located)
    finally:
        if own_client:
            client.close()

    west, south, east, north = REGION
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=out, suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as f:
            writer = Writer(f)
            for tile_id in sorted(tiles):
                writer.write_tile(tile_id, tiles[tile_id])
            writer.finalize(
                {
                    "tile_type": header["tile_type"],
                    "tile_compression": header["tile_compression"],
                    "min_lon_e7": int(west * 1e7),
                    "min_lat_e7": int(south * 1e7),
                    "max_lon_e7": int(east * 1e7),
                    "max_lat_e7": int(north * 1e7),
                    "center_zoom": 12,
                    "center_lon_e7": int(-79.385 * 1e7),
                    "center_lat_e7": int(43.6585 * 1e7),
                },
                {
                    **{k: v for k, v in metadata.items() if k in ("vector_layers", "version")},
                    "name": "Toronto (Torontrack extract)",
                    "attribution": ATTRIBUTION,
                    "source_build": build_key,
                },
            )
        digest = hashlib.sha256(Path(tmp).read_bytes()).hexdigest()
        os.replace(tmp, out / FILE_NAME)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    result = {
        "file": FILE_NAME,
        "source_build": build_key,
        "built_at": datetime.now(UTC).isoformat(),
        "bounds": list(BOUNDS),
        "max_zoom": MAX_ZOOM,
        "region_bounds": list(REGION),
        "region_max_zoom": REGION_MAX_ZOOM,
        "tiles": len(tiles),
        "bytes": (out / FILE_NAME).stat().st_size,
        "sha256": digest,
        "range_requests": source.requests,
        "attribution": ATTRIBUTION,
    }
    (out / "toronto.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
