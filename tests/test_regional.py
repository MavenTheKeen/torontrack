"""Regional trains: the schedule subset, timetable positions and departures."""

import io
import zipfile
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from transit import regional
from transit.db import connect
from transit.ttc.realtime import TORONTO
from transit.web import regional as web_regional
from transit.web.app import create_app

UNION = (43.645, -79.38)
EXHIBITION = (43.636, -79.418)
MIMICO = (43.616, -79.497)


def at(hhmmss: str) -> datetime:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return datetime(2026, 10, 13, h, m, s, tzinfo=TORONTO)


def go_zip() -> bytes:
    files = {
        "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type,"
        "route_color,route_text_color\n"
        "01-LW,GO,LW,Lakeshore West,2,98002e,FFFFFF\n"
        "01-21,GO,21,Milton Bus,3,,\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n"
        f"UN,Union Station GO,{UNION[0]},{UNION[1]}\n"
        f"EX,Exhibition GO,{EXHIBITION[0]},{EXHIBITION[1]}\n"
        f"MI,Mimico GO,{MIMICO[0]},{MIMICO[1]}\n"
        "00123,Bus Terminal,43.6,-79.6\n",
        "trips.txt": "route_id,service_id,trip_id,trip_headsign,direction_id,shape_id\n"
        "01-LW,20261013,T1,LW - Mimico GO,0,OUT\n"
        "01-LW,20261013,T2,LW - Union Station GO,1,IN\n"
        "01-21,20261013,B1,21 - Bus Terminal,0,\n",
        # Descending within each trip, as Metrolinx publishes it.
        "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "T1,08:14:00,08:14:00,MI,3\n"
        "T1,08:06:00,08:07:00,EX,2\n"
        "T1,08:00:00,08:01:00,UN,1\n"
        "T2,08:35:00,08:35:00,UN,3\n"
        "T2,08:27:00,08:28:00,EX,2\n"
        "T2,08:20:00,08:20:00,MI,1\n"
        "B1,08:00:00,08:00:00,00123,1\n"
        "B1,08:30:00,08:30:00,UN,2\n",
        "shapes.txt": "shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence\n"
        + "".join(
            f"{shape},{lat},{lon},{i}\n"
            for shape, points in (
                ("OUT", [UNION, EXHIBITION, MIMICO]),
                ("IN", [MIMICO, EXHIBITION, UNION]),
            )
            for i, (lat, lon) in enumerate(points)
        ),
        "calendar_dates.txt": "service_id,date,exception_type\n20261013,20261013,1\n",
        "feed_info.txt": "feed_publisher_name,feed_version\nMetrolinx,20261005110625\n",
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return out.getvalue()


def test_parse_keeps_trains_only_and_drops_the_trademark_from_names():
    feed = regional.parse(go_zip())
    assert [line["code"] for line in feed["lines"]] == ["LW"]
    assert sorted(s["name"] for s in feed["stations"]) == ["Exhibition", "Mimico", "Union Station"]
    assert {t["trip_id"]: t["destination"] for t in feed["trips"]} == {
        "T1": "Mimico",
        "T2": "Union Station",
    }
    assert [r[1:3] for r in feed["stop_times"] if r[0] == "T1"] == [(1, "UN"), (2, "EX"), (3, "MI")]
    assert feed["service_start"] == feed["service_end"] == datetime(2026, 10, 13).date()
    assert regional.clean_name("Hamilton GO Centre") == "Hamilton Centre"
    assert regional.clean_name("Bramalea GO Station") == "Bramalea"


@pytest.mark.integration
def test_ended_schedules_are_pruned_but_the_newest_is_kept(settings):
    feed = regional.parse(go_zip())
    with connect(settings.database_url) as conn:
        # An earlier, fully loaded schedule that ended in September.
        older = {**feed, "feed_version": "old", "service_end": datetime(2026, 9, 1).date()}
        regional.load(conn, older, "test://go", at("05:00:00"))
        regional.load(conn, feed, "test://go", at("06:00:00"))
        assert regional.prune(conn, at("08:00:00").date()) == 1
        # Even an ended schedule stays when it is the newest one there is.
        assert regional.prune(conn, datetime(2027, 1, 1).date()) == 0
        assert conn.execute("SELECT count(*) AS n FROM normalized.regional_stop_times").fetchone()[
            "n"
        ] == len(feed["stop_times"])


def test_feed_without_trains_is_rejected():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in regional.REQUIRED_FILES:
            archive.writestr(name, "route_id,route_type\n1,3\n")
    with pytest.raises(regional.FeedValidationError, match="no rail routes"):
        regional.parse(out.getvalue())


@pytest.fixture
def idx(settings):
    with connect(settings.database_url) as conn:
        feed = regional.parse(go_zip())
        assert regional.load(conn, feed, "test://go", at("06:00:00"))
        assert not regional.load(conn, feed, "test://go", at("06:00:00"))
        version = web_regional.current_version(conn, at("08:00:00").date())
        return web_regional.index(conn, version)


@pytest.mark.integration
def test_trains_are_placed_by_timetable_along_the_track(idx):
    assert web_regional.trains(idx, at("07:59:00"))["trains"] == []
    [moving] = web_regional.trains(idx, at("08:03:30"))["trains"]
    # Halfway in time from Union (dep 08:01) to Exhibition (arr 08:06), along the track.
    assert (moving["lat"], moving["lon"]) == pytest.approx(
        ((UNION[0] + EXHIBITION[0]) / 2, (UNION[1] + EXHIBITION[1]) / 2), abs=1e-4
    )
    assert moving["destination"] == "Mimico" and moving["next_station"] == "Exhibition"
    assert 240 < moving["bearing"] < 270  # West-southwest, towards Exhibition.
    # Its track ahead starts where it is, every 10 s for two minutes, ending at 08:05:30,
    # 270 of the 300 s from Union to Exhibition.
    track = moving["track"]
    assert track[0][:2] == pytest.approx([moving["lon"], moving["lat"]], abs=1e-4)
    assert [t[2] - track[0][2] for t in track] == list(range(0, 130, 10))
    assert track[-1][:2] == pytest.approx(
        [UNION[1] + 0.9 * (EXHIBITION[1] - UNION[1]), UNION[0] + 0.9 * (EXHIBITION[0] - UNION[0])],
        abs=1e-4,
    )
    [dwelling] = web_regional.trains(idx, at("08:06:30"))["trains"]
    assert dwelling["at_station"] == "Exhibition" and dwelling["next_station"] == "Mimico"
    [inbound] = web_regional.trains(idx, at("08:31:30"))["trains"]
    assert inbound["id"] == "T2" and 60 < inbound["bearing"] < 90


@pytest.mark.integration
def test_departures_skip_trains_that_end_at_the_station(idx):
    union = web_regional.station(idx, "UN", at("07:50:00"))
    assert [(d["trip_id"], d["minutes"]) for d in union["departures"]] == [("T1", 11)]
    exhibition = web_regional.station(idx, "EX", at("08:10:00"))
    assert [(d["destination"], d["minutes"]) for d in exhibition["departures"]] == [
        ("Union Station", 18)
    ]
    assert web_regional.station(idx, "ZZ", at("08:00:00")) is None


@pytest.mark.integration
def test_endpoints_credit_metrolinx_and_say_positions_are_scheduled(settings, idx):
    client = TestClient(create_app(settings, clock=lambda: at("08:03:30")))
    network = client.get("/api/regional").json()
    assert "permission of Metrolinx" in network["attribution"]
    [line] = network["lines"]
    assert (line["code"], line["colour"], len(line["paths"])) == ("LW", "#98002e", 1)
    assert {s["id"]: s["lines"] for s in network["stations"]} == {
        "UN": ["LW"],
        "EX": ["LW"],
        "MI": ["LW"],
    }
    trains = client.get("/api/regional/trains").json()
    assert trains["source"] == "timetable" and len(trains["trains"]) == 1
    assert client.get("/api/regional/stations/EX").json()["station"]["name"] == "Exhibition"
    assert client.get("/api/regional/stations/ZZ").status_code == 404
    assert client.get("/api/regional/stations/" + "x" * 20).status_code == 404
