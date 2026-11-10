"""City of Toronto business licences of food places: reading, loading and matching."""

import csv
import io
import json
from datetime import UTC, date, datetime

import pytest

from transit import food_inspections, food_licences
from transit.db import connect

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)
FIELDS = [
    "_id", "Category", "Licence No.", "Operating Name", "Issued", "Client Name",
    "Business Phone", "Business Phone Ext.", "Licence Address Line 1", "Licence Address Line 2",
    "Licence Address Line 3", "Ward", "Conditions", "Free Form Conditions Line 1",
    "Free Form Conditions Line 2", "Plate No.", "Endorsements", "Cancel Date",
    "Last Record Update",
]  # fmt: skip


def licence(number, name, address, issued, category="EATING OR DRINKING ESTABLISHMENT", **extra):
    base = {field: "" for field in FIELDS}
    base.update({
        "Category": category, "Licence No.": number, "Operating Name": name, "Issued": issued,
        "Client Name": "A HOLDER", "Business Phone": "416-555-0100",
        "Licence Address Line 1": address, "Licence Address Line 2": "TORONTO, ON",
    })  # fmt: skip
    return {**base, **extra}


def export(rows) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


ROWS = [
    # Cafe One under its first owner, then a new owner keeping the name.
    licence("B01", "CAFE ONE", "1 FRONT ST W, UNIT 2", "1998-05-01",
            **{"Cancel Date": "2015-03-01"}),
    licence("B02", "CAFE ONE INC", "1 FRONT ST W", "2015-03-02"),
    licence("B03", "SOMETHING ELSE", "1 FRONT ST W", "1980-01-01"),
    licence("B04", "", "1 FRONT ST W", "1970-01-01"),
    licence("B05", "TAXI", "1 FRONT ST W", "1960-01-01", category="TAXICAB OWNER"),
    licence("", "NO NUMBER", "2 FRONT ST W", "2000-01-01"),
    licence("B06", "BAD DATE", "3 FRONT ST W", "soon"),
]  # fmt: skip


def test_address_key_drops_unit_postal_code_and_punctuation():
    key = food_licences.address_key
    assert key("2300 YONGE ST, S106") == "2300 YONGE ST"
    assert key("1871 O'Connor Dr M4A 1X1") == "1871 OCONNOR DR"
    assert key("1 Front St W M5J1E6") == "1 FRONT ST W"


def test_food_licences_are_read_without_holder_details():
    licences, skipped = food_licences.read(io.StringIO(export(ROWS)))
    assert sorted(licences) == ["B01", "B02", "B03", "B04"]
    assert skipped == 2
    assert licences["B01"] == (
        "B01",
        "EATING OR DRINKING ESTABLISHMENT",
        "CAFE ONE",
        "1 FRONT ST W",
        date(1998, 5, 1),
        date(2015, 3, 1),
    )
    assert licences["B04"][2] is None
    assert not any("HOLDER" in str(v) or "416" in str(v) for v in licences.values())


class FakeClient:
    def __init__(self, bodies):
        self.bodies = list(bodies)

    def fetch_bytes(self, url, *, host, max_bytes=0, sink=None):
        if "package_show" in url:
            resources = [{"id": "table", "datastore_active": True}]
            return json.dumps({"result": {"resources": resources}}).encode()
        assert url.endswith("/datastore/dump/table")
        sink.write(self.bodies.pop(0).encode())
        return b""


@pytest.mark.integration
def test_refresh_and_licensed_since(settings, monkeypatch):
    from fastapi.testclient import TestClient

    from transit.config import Settings
    from transit.web.app import create_app

    monkeypatch.setattr(food_licences, "MIN_LICENCES", 1)
    url = settings.database_url
    body = export(ROWS)
    assert food_licences.refresh(url, FakeClient([body]), NOW)["licences"] == 4
    assert food_licences.refresh(url, FakeClient([body]), NOW)["status"] == "unchanged"
    assert food_licences.refresh(url, FakeClient([export([])]), NOW)["status"] == "failed"
    premises = {
        "E1": {"premises_id": "E1", "name": "Cafe One", "address": "1 Front St W M5J 1E6",
               "lat": 43.6455, "lon": -79.3807},
        "E2": {"premises_id": "E2", "name": "Bakery Two", "address": "1 Front St W M5J 1E6",
               "lat": 43.6455, "lon": -79.3807},
    }  # fmt: skip
    with connect(url) as conn:
        food_inspections.load(conn, premises, {("E1", date(2026, 10, 1)): "Pass"}, [], NOW)
        # The earliest matching name at the address; other names and blank names do not count.
        assert food_licences.since_listed(conn) == {"E1": date(1998, 5, 1)}
    client = TestClient(create_app(Settings(url), clock=lambda: NOW))
    assert client.get("/api/food/E1").json()["licensed_since"] == "1998-05-01"
    assert client.get("/api/food/E2").json()["licensed_since"] is None
