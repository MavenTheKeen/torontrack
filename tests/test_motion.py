"""Moving trains and vehicles between refreshes (static/motion.js), in a headless browser.

Skipped where Playwright's browser is not installed.
"""

from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

STATIC = Path(__file__).resolve().parents[1] / "src" / "transit" / "web" / "static"
T0 = 1_760_000_000  # Epoch seconds of a vehicle report.

# A straight pattern north along a street: three stops 0.002 degrees (~222 m) apart and
# 60 timetable seconds apart.
PATTERN = [[-79.4, 43.650, 0], [-79.4, 43.652, 60], [-79.4, 43.654, 120]]


@pytest.fixture(scope="module")
def page():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # Browser binaries not installed.
            pytest.skip(f"Chromium unavailable: {exc}")
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(content=(STATIC / "motion.js").read_text(encoding="utf-8"))
        yield page
        browser.close()


def test_a_train_runs_between_stations_to_its_predicted_arrival(page):
    positions = page.evaluate(
        """() => {
            const train = { position: 3.5, from_index: 3, next_index: 4, segment_seconds: 100,
                            arrives_at: new Date(1000 * 200).toISOString() };
            // 150 s before arrival it has not left; 50 s before it is half way; after the
            // predicted arrival it waits at the station.
            return [50, 150, 199, 260].map((t) => trainPosition(train, 1000 * t));
        }"""
    )
    assert positions[0] == 3 and positions[1] == 3.5 and positions[3] == 4
    assert 3.98 < positions[2] < 4
    # Without a run to follow (first station of the line), the server's position stands.
    assert page.evaluate("trainPosition({ position: 7, from_index: null }, 0)") == 7


def place(page, vehicle, seconds_after):
    return page.evaluate(
        "([v, pattern, t]) => vehiclePlace(v, preparePattern(pattern), t)",
        [vehicle, PATTERN, (T0 + seconds_after) * 1000],
    )


def vehicle(**extra):
    # Reported 10 m east of the route line, a third of the way to the second stop.
    return {
        "lon": -79.39988, "lat": 43.65067, "bearing": 0, "reported": T0, "stopped": False,
        "position": 1 / 3, "pace": 1.0, **extra,
    }  # fmt: skip


def test_a_bus_moves_along_its_route_from_its_report(page):
    moved = place(page, vehicle(), 40)
    # 40 timetable seconds on, at pace 1: two thirds of the way from stop 1 to stop 2,
    # keeping the report's 10 m offset east of the line.
    assert moved["coord"][0] == pytest.approx(-79.39988)
    assert moved["coord"][1] == pytest.approx(43.65067 + 0.002 * 2 / 3, abs=1e-6)
    assert moved["bearing"] == 0 and moved["fade"] == 1
    slow = place(page, vehicle(pace=0.5), 40)
    assert slow["coord"][1] == pytest.approx(43.65067 + 0.002 / 3, abs=1e-6)


def test_a_stopped_bus_holds_then_moves_and_an_old_report_stops_and_fades(page):
    assert place(page, vehicle(stopped=True), 15)["coord"] == [-79.39988, 43.65067]
    after = place(page, vehicle(stopped=True), 30)
    assert after["coord"][1] == pytest.approx(43.65067 + 0.002 / 6, abs=1e-6)
    # Past MAX_MOVE_S the estimate stops at the end of the pattern here; past FADE_AFTER_S
    # it fades.
    late, later = place(page, vehicle(), 130), place(page, vehicle(), 170)
    assert late["coord"] == later["coord"] and late["fade"] == 1 and later["fade"] < 1
    # A vehicle not matched to a route stays where it reported.
    unmatched = page.evaluate(
        "(v) => vehiclePlace(v, null, v.reported * 1000 + 60000)", vehicle(position=None)
    )
    assert unmatched["coord"] == [-79.39988, 43.65067]


def test_a_regional_train_follows_its_timetable_track(page):
    track = [[-79.5, 43.7, 100], [-79.4, 43.7, 110], [-79.4, 43.8, 130]]
    points = page.evaluate(
        "(track) => [90, 105, 120, 200].map((t) => trackPoint(track, t * 1000))", track
    )
    assert points[0] == [-79.5, 43.7] and points[3] == [-79.4, 43.8]
    assert points[1] == pytest.approx([-79.45, 43.7])
    assert points[2] == pytest.approx([-79.4, 43.75])


def test_corrections_glide_instead_of_jumping(page):
    steps = page.evaluate(
        """() => {
            const state = newGlide();
            const out = [glide(state, "a", [0, 0], 0)];
            state.seq += 1;  // New data puts the vehicle 1 unit east.
            out.push(glide(state, "a", [1, 0], 1000));
            out.push(glide(state, "a", [1, 0], 2000));
            out.push(glide(state, "a", [1, 0], 3500));
            return out;
        }"""
    )
    assert steps[0] == [0, 0] and steps[1] == [0, 0]
    assert 0 < steps[2][0] < 1 and steps[3] == [1, 0]


def test_heading_arrows_turn_instead_of_snapping(page):
    headings = page.evaluate(
        """() => {
            const state = newGlide();
            glide(state, "a", [0, 0], 0);
            const out = [turn(state, "a", 350, 0)];
            // The route turns to 80 degrees: the shorter way round is 90 degrees clockwise,
            // through north, at 180 degrees a second.
            out.push(turn(state, "a", 80, 250));
            out.push(turn(state, "a", 80, 1000));
            return out;
        }"""
    )
    assert headings == [350, 35, 80]
