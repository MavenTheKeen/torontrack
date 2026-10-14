"""The street-map extract: only tiles inside Toronto's bounds, read with range requests."""

import io
import json
import re

import httpx
import pytest
from pmtiles.reader import MmapSource, Reader
from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import Writer

from transit import maptiles
from transit.http import FetchError

# Tiles in the fake planet: the whole world, downtown Toronto at z15, and Vancouver.
WORLD = (0, 0, 0)
UNION = (15, 9158, 11959)
VANCOUVER = (15, 5177, 11214)


def planet() -> bytes:
    out = io.BytesIO()
    writer = Writer(out)
    for z, x, y in sorted([WORLD, UNION, VANCOUVER], key=lambda t: zxy_to_tileid(*t)):
        writer.write_tile(zxy_to_tileid(z, x, y), f"tile {z}/{x}/{y}".encode())
    writer.finalize(
        {
            "tile_type": TileType.MVT,
            "tile_compression": Compression.GZIP,
            "min_lon_e7": -1800000000,
            "min_lat_e7": -850000000,
            "max_lon_e7": 1800000000,
            "max_lat_e7": 850000000,
            "center_zoom": 0,
            "center_lon_e7": 0,
            "center_lat_e7": 0,
        },
        {"vector_layers": [{"id": "roads"}], "version": "4.0.0"},
    )
    return out.getvalue()


def serve(data: bytes):
    requests = []

    def handle(request):
        requests.append(str(request.url))
        if request.url.host == "build-metadata.protomaps.dev":
            builds = [
                {"key": "20261012.pmtiles", "uploaded": "2026-10-12T10:00:00Z"},
                {"key": "20261013.pmtiles", "uploaded": "2026-10-13T10:00:00Z"},
            ]
            return httpx.Response(200, json=builds)
        start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["Range"]).groups())
        return httpx.Response(206, content=data[start : end + 1])

    return httpx.Client(transport=httpx.MockTransport(handle)), requests


def test_tile_ranges_cover_toronto_at_every_zoom():
    ids = maptiles.tiles_in_bounds()
    assert zxy_to_tileid(*WORLD) in ids and zxy_to_tileid(*UNION) in ids
    assert zxy_to_tileid(*VANCOUVER) not in ids
    assert len(ids) == len(set(ids))


def test_extract_keeps_only_tiles_in_bounds(tmp_path):
    client, requests = serve(planet())
    result = maptiles.build(str(tmp_path), client)
    assert result["source_build"] == "20261013.pmtiles"  # The newest build.
    assert result["tiles"] == 2
    assert requests[1].startswith("https://build.protomaps.com/20261013.pmtiles")
    with open(tmp_path / "toronto.pmtiles", "rb") as f:
        reader = Reader(MmapSource(f))
        assert reader.get(*UNION) == b"tile 15/9158/11959"
        assert reader.get(*VANCOUVER) is None
        assert reader.metadata()["attribution"] == maptiles.ATTRIBUTION
    sidecar = json.loads((tmp_path / "toronto.json").read_text())
    assert sidecar["bytes"] == (tmp_path / "toronto.pmtiles").stat().st_size
    assert not list(tmp_path.glob("*.tmp"))


def test_builds_come_only_from_the_protomaps_host():
    with pytest.raises(FetchError):
        maptiles.Source("https://example.com/planet.pmtiles", httpx.Client())
