"""Toronto beach water quality: reading samples and each beach's daily result."""

import json
from datetime import date

import pytest

from transit import beaches
from transit.db import connect


def sample(beach, name, site, day, ecoli, lon=-79.4, lat=43.63):
    return {"beachId": str(beach), "beachName": name, "siteName": site,
            "collectionDate": day, "eColi": ecoli,
            "geometry": json.dumps({"type": "Point", "coordinates": [lon, lat]})}  # fmt: skip


ROWS = [
    # Sunnyside on 8 July: sites at 50 and 200, geometric mean 100, meeting the standard.
    sample(1, "Sunnyside Beach", "1", "2026-07-08", "50"),
    sample(1, "Sunnyside Beach", "2", "2026-07-08", "200"),
    # On 9 July: 100 and 400, geometric mean 200, over the standard.
    sample(1, "Sunnyside Beach", "1", "2026-07-09", "100"),
    sample(1, "Sunnyside Beach", "2", "2026-07-09", "400"),
    sample(2, "Woodbine Beach", "1", "2026-07-09", "0", lon=-79.3),
    sample(2, "Woodbine Beach", "1", "2026-07-09", "None"),
]


def test_samples_without_a_count_are_skipped():
    samples = beaches.read(ROWS)
    assert len(samples) == 5
    assert samples[0] == (1, "Sunnyside Beach", "1", date(2026, 7, 8), 50, -79.4, 43.63)


@pytest.mark.integration
def test_status_uses_the_daily_geometric_mean(settings):
    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO normalized.beach_samples (beach_id, beach_name, site, day,
                       ecoli, lon, lat) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                beaches.read(ROWS),
            )
        found = beaches.status(conn, date(2026, 7, 10))
        later = beaches.status(conn, date(2026, 9, 1))
    assert found["in_season"] and found["latest"] == date(2026, 7, 9)
    sunnyside, woodbine = found["beaches"]
    assert [(d["day"].day, d["ecoli"], d["safe"]) for d in sunnyside["recent"]] == [
        (9, 200, False),
        (8, 100, True),
    ]
    assert woodbine["recent"][0]["ecoli"] == 1  # A count of 0 is taken as 1.
    assert not later["in_season"]
