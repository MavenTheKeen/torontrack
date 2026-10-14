"""The City of Toronto's cycling network as a map layer, written next to the street map.

The city publishes it as GeoJSON ("Cycling Network", updated about twice a year): about
1,600 segments, each with the infrastructure on each side of the street. The layer keeps
only geometry and a rider-facing class, from the better side (INFRA_HIGHORDER):

- protected: cycle tracks, physically separated from traffic;
- lane: painted bike lanes, including buffered and contraflow lanes;
- trail: multi-use trails and park roads;
- shared: sharrows and signed routes without lane markings.

Empty fields in the source are a single space, and some nulls are the HTML-escaped text
"&lt;Null&gt;"; both are treated as missing.

Contains information licensed under the Open Government Licence – Toronto.
"""

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "cycling-network"
RESOURCE = "cycling-network - 4326.geojson"
FILE_NAME = "bike-lanes.geojson"
CLASSES = (
    ("protected", ("Cycle Track", "Bi-Directional Cycle Track")),
    ("lane", ("Bike Lane",)),
    ("trail", ("Multi-Use Trail", "Park Road")),
    ("shared", ("Sharrows", "Signed Route")),
)
MISSING = {"", "&lt;Null&gt;", "<Null>", "None"}


def classify(infra) -> str | None:
    """'Bike Lane - Contraflow' -> 'lane'; unknown or missing -> None."""
    text = (infra or "").strip()
    if text in MISSING:
        return None
    for name, prefixes in CLASSES:
        if any(text == p or text.startswith(f"{p} ") for p in prefixes):
            return name
    return None


def _text(value):
    value = (value or "").strip() if isinstance(value, str) else value
    return None if value in MISSING else value


def layer(source: dict) -> tuple[dict, dict]:
    """(GeoJSON layer, counts by class including skipped)."""
    features, counts = [], {}
    for f in source.get("features", []):
        props, geometry = f.get("properties") or {}, f.get("geometry") or {}
        kind = classify(props.get("INFRA_HIGHORDER")) or classify(props.get("INFRA_LOWORDER"))
        if kind is None or geometry.get("type") not in ("LineString", "MultiLineString"):
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        lines = geometry["coordinates"]
        if geometry["type"] == "LineString":
            lines = [lines]
        lines = [[[round(x, 5), round(y, 5)] for x, y, *_ in line] for line in lines]
        lines = [line for line in lines if len(line) >= 2]
        if not lines:
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        counts[kind] = counts.get(kind, 0) + 1
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "MultiLineString", "coordinates": lines},
                "properties": {
                    "kind": kind,
                    "street": _text(props.get("STREET_NAME")),
                    "infra": _text(props.get("INFRA_HIGHORDER")),
                    "installed": _text(props.get("INSTALLED")),
                },
            }
        )
    if not features:
        raise FetchError("Cycling network has no usable segments")
    return {"type": "FeatureCollection", "features": features}, counts


def build(out_dir: str, client: FeedClient | None = None) -> dict:
    own = client is None
    client = client or FeedClient()
    try:
        meta = json.loads(
            client.fetch_bytes(
                f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}",
                host=STATIC_HOST,
            )
        )
        urls = {r["name"]: r["url"] for r in meta["result"]["resources"]}
        if RESOURCE not in urls:
            raise FetchError(f"Cycling network resource {RESOURCE!r} not found")
        source = json.loads(
            client.fetch_bytes(urls[RESOURCE], host=STATIC_HOST, max_bytes=50 << 20)
        )
    finally:
        if own:
            client.close()
    geojson, counts = layer(source)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=out, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(geojson, f, separators=(",", ":"))
        os.replace(tmp, out / FILE_NAME)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    result = {
        "file": FILE_NAME,
        "built_at": datetime.now(UTC).isoformat(),
        "segments": counts,
        "bytes": (out / FILE_NAME).stat().st_size,
        "source": urls[RESOURCE],
    }
    (out / "bike-lanes.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
