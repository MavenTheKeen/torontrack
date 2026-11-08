"""City of Toronto food inspections (DineSafe): reading the export and loading it."""

import csv
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from transit import food_inspections
from transit.db import connect
from transit.web import pipeline

NOW = datetime(2026, 11, 8, 15, 0, tzinfo=UTC)
FIELDS = [
    "_id", "unique_id", "estId", "oldEstId", "estName", "address", "inspectionStatus",
    "phone", "inspectionDate", "observation", "typeDesc", "deficiencyDesc", "severity",
    "OutcomeDate", "OutcomeDesc", "amountFined", "latitude", "longitude",
]  # fmt: skip


def row(uid, est="E1", day="2026-10-01", status="Pass", infraction="None", **extra) -> dict:
    base = {
        "_id": "1", "unique_id": uid, "estId": est, "oldEstId": "1", "estName": " Cafe One ",
        "address": "1 Front St W None M5J 1E6", "inspectionStatus": status, "phone": "",
        "inspectionDate": day, "observation": "", "typeDesc": infraction,
        "deficiencyDesc": "None", "severity": "None", "OutcomeDate": "", "OutcomeDesc": "None",
        "amountFined": "", "latitude": "43.6455", "longitude": "-79.3807",
    }  # fmt: skip
    return {**base, **extra}


def export(rows) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def test_inspections_and_infractions_are_read_and_cleaned():
    premises, inspections, infractions, skipped = food_inspections.read(
        io.StringIO(
            export(
                [
                    row("a"),  # An inspection with nothing found.
                    row(
                        "b",
                        day="2026-11-02",
                        status="Conditional Pass",
                        infraction="Fail to protect against entry of pests - Sec. 13(1)",
                        severity="S - Significant",
                        OutcomeDesc="Conviction: Fined",
                        OutcomeDate="2026-11-05",
                        amountFined="555.00",
                    ),
                    row("c", est="E2", latitude="45.42"),  # Ottawa: skipped.
                    row("d", est="E3", inspectionDate="soon"),  # No date: skipped.
                ]
            )
        )
    )
    assert premises == {
        "E1": {
            "premises_id": "E1",
            "name": "Cafe One",
            "address": "1 Front St W M5J 1E6",
            "lat": 43.6455,
            "lon": -79.3807,
        }
    }
    assert inspections == {
        ("E1", date(2026, 10, 1)): "Pass",
        ("E1", date(2026, 11, 2)): "Conditional Pass",
    }
    assert infractions == [
        (
            "b",
            "E1",
            date(2026, 11, 2),
            "Fail to protect against entry of pests - Sec. 13(1)",
            None,
            "significant",
            "Conviction - Fined",
            date(2026, 11, 5),
            Decimal("555.00"),
        )
    ]
    assert skipped == 2


class FakeClient:
    def __init__(self, bodies):
        self.bodies = list(bodies)

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None):
        if "package_show" in url:
            resources = [
                {"id": "csv", "datastore_active": False},
                {"id": "table", "datastore_active": True},
            ]
            return json.dumps({"result": {"resources": resources}}).encode()
        assert url.endswith("/datastore/dump/table")
        sink.write(self.bodies.pop(0).encode())
        return b""


@pytest.mark.integration
def test_refresh_loads_replaces_infractions_and_keeps_old_inspections(settings, monkeypatch):
    monkeypatch.setattr(food_inspections, "MIN_PREMISES", 1)
    first = export(
        [
            row("a", day="2025-01-10", infraction="Old finding", severity="M - Minor"),
            row("b", day="2026-10-01", infraction="Dirty floor", severity="M - Minor"),
        ]
    )
    # The 2025 inspection has left the file; the 2026 one was corrected to no infraction.
    second = export([row("c", day="2026-10-01")])
    client = FakeClient([first, first, second])
    url = settings.database_url
    assert food_inspections.refresh(url, client, NOW)["infractions"] == 2
    assert food_inspections.refresh(url, client, NOW)["status"] == "unchanged"
    assert food_inspections.refresh(url, client, NOW)["status"] == "loaded"
    with connect(url) as conn:
        kept = conn.execute(
            """SELECT i.inspected_on, count(f.*) AS found FROM normalized.food_inspections i
               LEFT JOIN normalized.food_infractions f USING (premises_id, inspected_on)
               GROUP BY 1 ORDER BY 1"""
        ).fetchall()
        assert [(r["inspected_on"], r["found"]) for r in kept] == [
            (date(2025, 1, 10), 1),
            (date(2026, 10, 1), 0),
        ]
        health = pipeline.food_inspections(conn, datetime.now(UTC))
    assert (health["status"], health["premises"]) == ("ok", 1)


@pytest.mark.integration
def test_a_truncated_export_is_not_loaded(settings):
    client = FakeClient([export([row("a")])])
    result = food_inspections.refresh(settings.database_url, client, NOW)
    assert result["status"] == "failed" and "only 1 premises" in result["error"]
    with connect(settings.database_url) as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM normalized.food_premises").fetchone()["n"] == 0
        )
