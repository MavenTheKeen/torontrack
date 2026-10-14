"""Trip guidance engine (static/guidance.js), run in a headless browser.

The engine is plain JavaScript with no page or map dependencies, so it is loaded into a blank
page and driven with positions. Skipped where Playwright's browser is not installed.
"""

import json
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

STATIC = Path(__file__).resolve().parents[1] / "src" / "transit" / "web" / "static"

# A walk east along a street, a two-stop bus ride north, then a short walk: small enough to
# follow by hand. Points are [lon, lat]; 0.001 degrees of latitude is about 111 m.
TRIP = {
    "start": "2026-10-14T09:00:00-04:00",
    "end": "2026-10-14T09:20:00-04:00",
    "legs": [
        {
            "mode": "walk",
            "network": None,
            "route": None,
            "to": "Main St at King",
            "start": "2026-10-14T09:00:00-04:00",
            "end": "2026-10-14T09:04:00-04:00",
            "from_point": [-79.4000, 43.6500],
            "to_point": [-79.3960, 43.6500],
            "path": [[-79.4000, 43.6500], [-79.3980, 43.6500], [-79.3960, 43.6500]],
            "steps": [
                {
                    "action": "depart",
                    "street": "King St",
                    "metres": 160,
                    "point": [-79.4000, 43.6500],
                },
                {
                    "action": "left",
                    "street": "Main St",
                    "metres": 160,
                    "point": [-79.3980, 43.6500],
                },
            ],
            "stops": [],
        },
        {
            "mode": "bus",
            "network": "ttc",
            "route": "72",
            "headsign": "Union Station",
            "from": "Main St at King",
            "to": "Main St at Queen",
            "from_stop_id": "1234",
            "start": "2026-10-14T09:06:00-04:00",
            "end": "2026-10-14T09:14:00-04:00",
            "from_point": [-79.3960, 43.6500],
            "to_point": [-79.3960, 43.6560],
            "path": [[-79.3960, 43.6500], [-79.3960, 43.6530], [-79.3960, 43.6560]],
            "steps": [],
            "stops": [
                {
                    "name": "Main St at Shuter",
                    "point": [-79.3960, 43.6530],
                    "arrival": "2026-10-14T09:10:00-04:00",
                }
            ],
        },
        {
            "mode": "walk",
            "network": None,
            "route": None,
            "to": None,
            "start": "2026-10-14T09:14:00-04:00",
            "end": "2026-10-14T09:20:00-04:00",
            "from_point": [-79.3960, 43.6560],
            "to_point": [-79.3940, 43.6560],
            "path": [[-79.3960, 43.6560], [-79.3940, 43.6560]],
            "steps": [],
            "stops": [],
        },
    ],
}


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # Browser binaries not installed.
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        for name in ("icons.js", "guidance.js"):
            page.add_script_tag(content=(STATIC / name).read_text(encoding="utf-8"))
        yield page
        browser.close()


def drive(page, fixes, trip=TRIP):
    """Feed fixes through a fresh guidance state; one result per fix."""
    return page.evaluate(
        """([trip, fixes]) => {
            const state = newGuidance(trip);
            return fixes.map(([lon, lat, accuracy]) => {
                const fix = { lon, lat, accuracy: accuracy ?? 5, heading: null, at: 0 };
                const info = updateGuidance(state, fix);
                return {
                    phase: info.phase, title: info.title, legIndex: info.legIndex,
                    offRoute: info.offRoute, stopsLeft: info.stopsLeft ?? null,
                    distance: info.distance === undefined ? null : Math.round(info.distance),
                };
            });
        }""",
        [trip, fixes],
    )


def test_walk_ride_and_arrive_in_order(page):
    results = drive(
        page,
        [
            [-79.3999, 43.6500],  # Setting off along King St.
            [-79.3970, 43.6500],  # Past the turn: walking to the stop.
            [-79.3960, 43.6501],  # At the stop.
            [-79.3960, 43.6515],  # On the bus, before the stop in between.
            [-79.3960, 43.6540],  # Past it: the next stop is ours.
            [-79.3960, 43.6559],  # Off the bus.
            [-79.3940, 43.6560],  # At the destination.
        ],
    )
    assert [r["phase"] for r in results] == [
        "walk",
        "walk",
        "wait",
        "ride",
        "ride",
        "walk",
        "arrived",
    ]
    assert results[0]["title"] == "Turn left onto Main St"
    assert 150 < results[0]["distance"] < 170
    assert results[1]["title"] == "Walk to Main St at King"
    assert results[2]["title"] == "Board 72 towards Union Station"
    assert [r["stopsLeft"] for r in results[3:5]] == [2, 1]
    assert results[4]["title"] == "Get off at the next stop: Main St at Queen"
    assert results[5]["title"] == "Walk to your destination"


def test_progress_never_goes_back(page):
    # A fix back at the start after boarding is GPS noise, not a return trip.
    results = drive(page, [[-79.3960, 43.6501], [-79.3960, 43.6520], [-79.4000, 43.6500]])
    assert [r["legIndex"] for r in results] == [1, 1, 1]
    assert results[-1]["phase"] == "ride"


def test_off_route_after_repeated_fixes_away_from_the_walk(page):
    away = [-79.3990, 43.6520]  # About 220 m north of King St.
    results = drive(page, [[-79.3999, 43.6500], away, away, away])
    assert [r["offRoute"] for r in results] == [False, False, False, True]


def test_simulated_source_follows_the_timetable(page):
    fixes = page.evaluate(
        """(trip) => new Promise((resolve) => {
            const seen = [];
            // Each 1 ms tick moves the simulated clock 10 minutes.
            const source = simulatedLocationSource(trip, { speedup: 600000, intervalMs: 1 });
            source.start((fix) => {
                seen.push([fix.lon, fix.lat, fix.at]);
                if (fix.at >= Date.parse(trip.end)) { source.stop(); resolve(seen); }
            });
        })""",
        TRIP,
    )
    times = [at for _, _, at in fixes]
    assert times == sorted(times) and len(fixes) == 3  # 0, 10 and 20 minutes in.
    lon, lat, _ = fixes[1]  # 09:10: on the bus, at the stop in between.
    assert round(lon, 4) == -79.396 and round(lat, 4) == 43.653
    assert json.dumps(fixes[-1][:2]) == "[-79.394, 43.656]"


def test_location_falls_back_to_network_positioning(page):
    # A desktop without GPS or Wi-Fi fails the high-accuracy request; the watch retries
    # without it. A declined permission is reported at once, with no retry.
    calls = page.evaluate(
        """() => {
            const run = (firstError) => {
                const calls = [];
                const fake = {
                    watchPosition(ok, fail, options) {
                        calls.push(options.enableHighAccuracy);
                        const id = calls.length;
                        setTimeout(() => (options.enableHighAccuracy
                            ? fail({ code: firstError })
                            : ok({ coords: { longitude: -79.4, latitude: 43.65 } })), 0);
                        return id;
                    },
                    clearWatch() {},
                };
                Object.defineProperty(
                    navigator, "geolocation", { value: fake, configurable: true });
                return new Promise((resolve) => watchLocation(
                    () => resolve({ calls, result: "position" }),
                    (e) => resolve({ calls, result: locationErrorText(e) }),
                ));
            };
            return run(2).then((a) => run(1).then((b) => [a, b]));
        }"""
    )
    unavailable, declined = calls
    assert unavailable == {"calls": [True, False], "result": "position"}
    assert declined["calls"] == [True]
    assert declined["result"].startswith("Location permission was declined")
