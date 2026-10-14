"""The city's cycling network as a map layer."""

from transit import bike_lanes


def test_cycling_network_is_classified_for_riders():
    assert bike_lanes.classify("Bike Lane - Contraflow") == "lane"
    assert bike_lanes.classify("Bi-Directional Cycle Track") == "protected"
    assert bike_lanes.classify("Multi-Use Trail - Entrance") == "trail"
    assert bike_lanes.classify("Sharrows - Wayfinding") == "shared"
    assert bike_lanes.classify(" ") is None
    assert bike_lanes.classify("&lt;Null&gt;") is None
    source = {
        "features": [
            {
                "properties": {
                    "INFRA_HIGHORDER": " ",
                    "INFRA_LOWORDER": "Cycle Track",
                    "STREET_NAME": "Bloor St W",
                    "INSTALLED": 2020,
                },
                "geometry": {
                    "type": "MultiLineString",
                    "coordinates": [[[-79.4123456, 43.66], [-79.41, 43.661]]],
                },
            },
            {"properties": {"INFRA_HIGHORDER": "Unknown thing"}, "geometry": {"type": "Point"}},
        ]
    }
    layer, counts = bike_lanes.layer(source)
    assert counts == {"protected": 1, "skipped": 1}
    [feature] = layer["features"]
    assert feature["properties"]["street"] == "Bloor St W"
    assert feature["geometry"]["coordinates"][0][0] == [-79.41235, 43.66]
