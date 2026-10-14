"""TTC's official delay log: station matching on real names, parsing, and refresh."""

import json
from datetime import datetime

import pytest

from tests.ttc_helpers import T0, static_zip
from transit.db import connect
from transit.http import FetchError
from transit.ttc import delays, static_gtfs
from transit.ttc.realtime import TORONTO
from transit.ttc.sources import STATIC_HOST

KEYS = {
    "bloor-yonge",
    "union",
    "kennedy",
    "st-george",
    "pioneer-village",
    "north-york-centre",
    "vaughan-metropolitan-centre",
    "tmu",
    "cedarvale",
    "sheppard-yonge",
    "sheppard-west",
    "st-clair-west",
    "dundas-west",
    "main-street",
    "wilson",
    "highway-407",
    "eglinton",
}


@pytest.mark.parametrize(
    ("text", "key", "how"),
    [
        # Real station fields from the published log.
        ("KENNEDY BD STATION", "kennedy", "exact"),
        ("ST GEORGE YUS STATION", "st-george", "exact"),
        ("PIONEER VILLAGE STATIO", "pioneer-village", "exact"),  # Cut at 22 characters.
        ("NORTH YORK CENTRE STAT", "north-york-centre", "exact"),
        ("KIPLING STATION - PLAT", None, "unmatched"),  # Not in this test's station list.
        ("YONGE BD STATION", "bloor-yonge", "alias"),
        ("BLOOR STATION", "bloor-yonge", "alias"),
        ("VMC STATION (PLATFORM", "vaughan-metropolitan-centre", "alias"),
        ("DUNDAS STATION", "tmu", "alias"),
        ("EGLINTON WEST STATION", "cedarvale", "alias"),
        ("SHEPPARD STATION", "sheppard-yonge", "alias"),
        ("SHEPPARD WEST STATION", "sheppard-west", "exact"),
        ("NORTH YORK CTR STATION", "north-york-centre", "alias"),
        ("ST. CLAIR WEST STATION", "st-clair-west", "exact"),
        ("APPROACHING DUNDAS WES", "dundas-west", "prefix"),
        ("WILSO STATION", "wilson", "prefix"),
        ("UNION STATION TO KING", "union", "range"),
        ("LINE 1 YUS - EGLINTON", "eglinton", "exact"),
        ("GREENWOOD YARD", None, "unmatched"),
        ("WILSON YARD", None, "unmatched"),
        ("", None, "unmatched"),
    ],
)
def test_station_names_from_the_log_match_station_keys(text, key, how):
    assert delays.match_station(text, KEYS) == (key, how)


def test_double_encoded_text_is_repaired_and_correct_text_left_alone():
    assert delays.repair_text("PAA â\u0080\u0093 NO TROUBLE FOUND") == "PAA – NO TROUBLE FOUND"
    assert delays.repair_text("café – ok") == "café – ok"


CSV = (
    "_id,Date,Time,Day,Station,Code,Min Delay,Min Gap,Bound,Line,Vehicle\n"
    "1,2026-10-10,06:05,Saturday,WELLESLEY STATION,MUSAN,5,9,S,YU,5227\n"
    "2,2026-10-10,23:55,Saturday,BLOOR STATION,MUIRS,0,0,,YU/BD,0\n"
)
CODES = "_id,CODE,DESCRIPTION\n1,MUSAN,UNSANITARY VEHICLE\n2,MUIRS,INJURED\n"


def test_rows_parse_with_local_times_and_known_lines():
    first, second = delays.parse_delays(CSV.encode(), {"wellesley", "bloor-yonge"})
    assert first["delay_at"] == datetime(2026, 10, 10, 6, 5, tzinfo=TORONTO)
    assert (first["route_id"], first["bound"], first["min_gap"]) == ("1", "Southbound", 9)
    assert (second["route_id"], second["bound"], second["station_key"]) == (
        None,
        None,
        "bloor-yonge",
    )


class CatalogueClient:
    """Serves the open-data catalogue entry and the two CSV files."""

    def __init__(self, csv=CSV, fail=False):
        self.csv, self.fail = csv, fail

    def fetch_bytes(self, url, *, host, max_bytes=None):
        assert host == STATIC_HOST
        if self.fail:
            raise FetchError("HTTP 503: retries exhausted")
        if "package_show" in url:
            resources = [
                {"name": delays.DELAYS_RESOURCE, "url": f"https://{STATIC_HOST}/d.csv"},
                {"name": delays.CODES_RESOURCE, "url": f"https://{STATIC_HOST}/c.csv"},
            ]
            return json.dumps({"result": {"resources": resources}}).encode()
        return (self.csv if url.endswith("d.csv") else CODES).encode()


@pytest.mark.integration
def test_refresh_loads_changes_only_and_records_every_attempt(settings):
    with connect(settings.database_url) as conn:
        static_gtfs.load(conn, static_gtfs.parse(static_zip()), "test://gtfs", T0)
    first = delays.refresh(settings.database_url, CatalogueClient())
    assert (first["status"], first["rows"], first["matched_with_gap"]) == ("loaded", 2, 1)
    assert delays.refresh(settings.database_url, CatalogueClient())["status"] == "unchanged"
    newer = CSV + "3,2026-10-11,07:00,Sunday,COLLEGE STATION,MUSAN,4,6,N,YU,5100\n"
    assert delays.refresh(settings.database_url, CatalogueClient(newer))["rows"] == 3
    assert delays.refresh(settings.database_url, CatalogueClient(fail=True))["status"] == "failed"
    with connect(settings.database_url) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM normalized.ttc_official_delays").fetchone()["n"]
            == 3
        )
        statuses = [
            r["status"]
            for r in conn.execute("SELECT status FROM ops.ttc_delay_refreshes ORDER BY refresh_id")
        ]
        assert statuses == ["loaded", "unchanged", "loaded", "failed"]
        description = conn.execute(
            "SELECT description FROM normalized.ttc_delay_codes WHERE code = 'MUSAN'"
        ).fetchone()["description"]
        assert description == "UNSANITARY VEHICLE"
