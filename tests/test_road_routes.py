"""Matching road restrictions to the bus and streetcar routes they lie along."""

from datetime import timedelta

import pytest

from tests.test_roads import NOW, Client, body, item
from tests.ttc_helpers import T0, static_zip
from transit import road_routes, roads
from transit.db import connect
from transit.ttc import matching, static_gtfs, surface
from transit.ttc.matching import _xy

# An east-west route along latitude 43.7, from -79.30 to -79.20 (about 8 km).
ROUTE = {"10": [{"direction_id": 0, "xy": [_xy(43.7, -79.30), _xy(43.7, -79.20)]}]}
GRID = road_routes._segments(ROUTE)


def test_a_restriction_running_along_the_route_matches_with_its_overlap():
    along = [[-79.26, 43.7001], [-79.2575, 43.7001]]  # ~200 m, 11 m off the route.
    [(key, metres)] = road_routes.overlaps(along, GRID).items()
    assert key == ("10", 0) and 190 <= metres <= 210


def test_a_cross_street_touching_the_route_does_not_match():
    across = [[-79.25, 43.6990], [-79.25, 43.7010]]  # 220 m north-south across it.
    assert road_routes.overlaps(across, GRID) == {}


def test_a_point_matches_only_when_close():
    assert road_routes.overlaps([[-79.25, 43.7002]], GRID) == {("10", 0): 0}
    assert road_routes.overlaps([[-79.25, 43.7010]], GRID) == {}


@pytest.mark.integration
def test_refresh_matches_new_restrictions_once(settings):
    with connect(settings.database_url) as conn:
        feed = static_gtfs.parse(static_zip())
        static_gtfs.load(conn, feed, "test://gtfs", T0)
        surface.load(conn, surface.parse(static_zip()), feed.feed_version)
    matching._index.clear()
    # Along Danforth Rd between Kennedy and Midland, where the 10 runs both ways.
    danforth = item("d1", geoPolyline="[-79.268,43.70015],[-79.262,43.70015]")
    elsewhere = item("x1")  # Cole St, far from any test route.
    client = Client([body([danforth, elsewhere]), body([danforth, elsewhere, item("x2")])])
    first = roads.refresh(settings.database_url, client, NOW)
    assert first["route_matches"] == {"checked": 2, "matched": 1}
    # Only the new restriction is checked on the next changed response.
    second = roads.refresh(settings.database_url, client, NOW + timedelta(minutes=10))
    assert second["route_matches"] == {"checked": 1, "matched": 0}
    with connect(settings.database_url) as conn:
        rows = conn.execute(
            """SELECT route_id, direction_id FROM normalized.road_restriction_routes
               ORDER BY direction_id"""
        ).fetchall()
        assert [(r["route_id"], r["direction_id"]) for r in rows] == [("10", 0), ("10", 1)]
        [active] = [r for r in roads.active(conn, NOW) if r["restriction_id"] == "d1"]
    assert active["routes"] == ["10"]
