"""CaféTO patios: curb-lane, sidewalk, parklet and private-property patios licensed under the
City of Toronto's CaféTO program, for the city map. The city refreshes the list monthly;
each load replaces it. Operator names are business names as licensed.

Contains information licensed under the Open Government Licence – Toronto.
"""

import json
from datetime import UTC, datetime

from transit.db import connect
from transit.food_inspections import display
from transit.http import FeedClient, FetchError
from transit.ttc.sources import STATIC_HOST

PACKAGE = "cafeto-curb-lane-parklet-cafe-locations"
MAX_BYTES = 20 << 20
MIN_PATIOS = 200


def read(rows: list[dict]) -> list[tuple]:
    """(licence number, operator, address, kind of patio, lon, lat) for each patio."""
    patios = []
    for row in rows:
        try:
            geometry = json.loads(row["geometry"])
            lon, lat = geometry["coordinates"][:2]
        except (KeyError, TypeError, ValueError):
            continue
        if geometry.get("type") != "Point":
            continue
        name = (row.get("OPERATOR_NAME") or "").strip()
        address = (row.get("MUNICIPAL_ADDRESS") or "").strip()
        kind = (row.get("INTERVENTION_TYPE") or "").strip()
        if not name or name == "None":
            continue
        patios.append(
            (
                str(row.get("LICENCE_NUMBER") or "").strip() or None,
                display(" ".join(name.split())),
                display(address) if address and address != "None" else None,
                kind or None,
                round(float(lon), 6),
                round(float(lat), 6),
            )
        )
    return patios


def refresh(database_url: str, client: FeedClient, now: datetime | None = None) -> dict:
    started = now or datetime.now(UTC)
    result = {"status": "failed"}
    with connect(database_url) as conn:
        try:
            meta = json.loads(
                client.fetch_bytes(
                    f"https://{STATIC_HOST}/api/3/action/package_show?id={PACKAGE}",
                    host=STATIC_HOST,
                )
            )
            [resource] = [r for r in meta["result"]["resources"] if r.get("datastore_active")][:1]
            body = json.loads(
                client.fetch_bytes(
                    f"https://{STATIC_HOST}/datastore/dump/{resource['id']}?format=json",
                    host=STATIC_HOST,
                    max_bytes=MAX_BYTES,
                )
            )
            fields = [f["id"] for f in body["fields"]]
            patios = read([dict(zip(fields, r, strict=True)) for r in body["records"]])
            if len(patios) < MIN_PATIOS:
                raise FetchError(f"Only {len(patios)} CaféTO patios; not loaded")
            with conn.transaction(), conn.cursor() as cur:
                cur.execute("DELETE FROM normalized.cafeto_patios")
                with cur.copy(
                    """COPY normalized.cafeto_patios (licence_no, operator, address, kind, lon,
                           lat) FROM STDIN"""
                ) as copy:
                    for row in patios:
                        copy.write_row(row)
            result.update(status="loaded", patios=len(patios))
        except (FetchError, KeyError, ValueError, TypeError) as exc:
            result["error"] = str(exc)[:1000]
        conn.execute(
            """INSERT INTO ops.cafeto_refreshes (started_at, finished_at, status, patios, error)
               VALUES (%s, now(), %s, %s, %s)""",
            (started, result["status"], result.get("patios"), result.get("error")),
        )
    return result


def listed(conn) -> list[list]:
    """[lon, lat, operator, address, kind] for every patio."""
    return [
        [r["lon"], r["lat"], r["operator"], r["address"], r["kind"]]
        for r in conn.execute(
            "SELECT lon, lat, operator, address, kind FROM normalized.cafeto_patios ORDER BY 3"
        )
    ]
