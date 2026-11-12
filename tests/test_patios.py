"""CaféTO patio locations: reading the city's table."""

import json

from transit import patios


def row(name, address="624 KING ST W", kind="Curb Lane", geometry=None):
    return {
        "LICENCE_NUMBER": "B97-1", "OPERATOR_NAME": name, "MUNICIPAL_ADDRESS": address,
        "INTERVENTION_TYPE": kind,
        "geometry": json.dumps(geometry or {"type": "Point", "coordinates": [-79.401, 43.6445]}),
    }  # fmt: skip


def test_patios_are_read_in_normal_case_and_unnamed_ones_skipped():
    found = patios.read([
        row("CASSIUS  CUCINA CONTEMPORANEA"),
        row("None"),
        row("Bar Raval", address="None", kind=""),
        row("NO POINT", geometry={"type": "Polygon", "coordinates": []}),
    ])  # fmt: skip
    assert found == [
        ("B97-1", "Cassius Cucina Contemporanea", "624 King St W", "Curb Lane", -79.401, 43.6445),
        ("B97-1", "Bar Raval", None, None, -79.401, 43.6445),
    ]
