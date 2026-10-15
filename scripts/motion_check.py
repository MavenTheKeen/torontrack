"""Measure how well the map moves buses and streetcars (docs/map.md#moving-vehicles).

    python scripts/motion_check.py accuracy [--minutes 10] [--url https://...]
    python scripts/motion_check.py smoothness [--seconds 30] [--url https://...]

accuracy: polls /api/vehicles every 10 s. Whenever a vehicle sends a new report, the
position the map would have estimated for that moment from the previous report (along its
pattern at the sent pace, as static/motion.js does) is compared with the new report.
Prints the error in metres by pace source.

smoothness: opens the map in a headless browser (Playwright) at street zoom, samples
every drawn vehicle every 100 ms and prints, per vehicle, the ratio of its fastest frame
to its median moving speed, and its fastest speed in m/s. A vehicle moving evenly has a
ratio near 1; a jump at a refresh shows as a high ratio.
"""

import argparse
import math
import statistics
import time

import httpx

SITE = "https://torontrack.skynetlab.duckdns.org"
M_PER_DEG = 111_000
LON_SCALE = 0.72  # cos(43.7 degrees).
# As in static/motion.js.
STOPPED_HOLD_S = 20
MAX_MOVE_S = 120


def metres(a, b):
    return math.hypot((b[0] - a[0]) * LON_SCALE * M_PER_DEG, (b[1] - a[1]) * M_PER_DEG)


def _offset_at(pattern, position):
    n = len(pattern)
    i = min(int(position), n - 1)
    j = min(i + 1, n - 1)
    return pattern[i][2] + (position - i) * (pattern[j][2] - pattern[i][2])


def _position_at_offset(pattern, offset):
    if offset >= pattern[-1][2]:
        return len(pattern) - 1
    for i in range(len(pattern) - 1):
        a, b = pattern[i][2], pattern[i + 1][2]
        if a <= offset <= b:
            return i + (0 if b == a else (offset - a) / (b - a))
    return 0


def _point(pattern, position):
    i = max(0, min(len(pattern) - 2, int(position)))
    f = max(0.0, min(1.0, position - i))
    a, b = pattern[i], pattern[i + 1]
    return [a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])]


def estimate(v, pattern, at):
    """Where motion.js places vehicle record `v` at epoch second `at`."""
    if not pattern or v["position"] is None:
        return [v["lon"], v["lat"]]
    moving = max(0, min(MAX_MOVE_S, at - v["reported"]) - (STOPPED_HOLD_S if v["stopped"] else 0))
    start = _point(pattern, v["position"])
    ahead = _position_at_offset(pattern, _offset_at(pattern, v["position"]) + v["pace"] * moving)
    end = _point(pattern, ahead)
    return [v["lon"] + end[0] - start[0], v["lat"] + end[1] - start[1]]


def accuracy(url, minutes):
    client = httpx.Client(timeout=30)
    patterns = client.get(f"{url}/api/patterns").json()["patterns"]
    last, errors = {}, {}
    until = time.time() + 60 * minutes
    while time.time() < until:
        data = client.get(f"{url}/api/vehicles").json()
        fields = data["fields"]
        for row in data["vehicles"]:
            v = dict(zip(fields, row, strict=True))
            prev = last.get(v["id"])
            if prev and v["reported"] > prev["reported"] and prev["pattern"] is not None:
                if v["pattern"] == prev["pattern"] and v["reported"] - prev["reported"] <= 90:
                    guess = estimate(prev, patterns.get(str(prev["pattern"])), v["reported"])
                    source = prev.get("pace_source") or "pace"
                    errors.setdefault(source, []).append(metres(guess, [v["lon"], v["lat"]]))
            if not prev or v["reported"] > prev["reported"]:
                last[v["id"]] = v
        time.sleep(10)
    for source, errs in sorted(errors.items()):
        errs.sort()
        q = [round(errs[int(len(errs) * f)]) for f in (0.5, 0.75, 0.9)]
        print(f"{source}: {len(errs)} reports, error m p50 {q[0]}, p75 {q[1]}, p90 {q[2]}")


def smoothness(url, seconds):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-gl=swiftshader", "--ignore-gpu-blocklist"])
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.goto(f"{url}/#/map", wait_until="networkidle")
        page.wait_for_timeout(5000)
        page.evaluate("activeMap.map.jumpTo({ center: [-79.40, 43.66], zoom: 15 })")
        page.wait_for_timeout(15000)
        samples = page.evaluate(
            """async (n) => { const out = [];
              for (let i = 0; i < n; i++) {
                await new Promise((r) => setTimeout(r, 100));
                out.push([performance.now(), Object.fromEntries((activeMap.vehicleFeatures || [])
                  .map((f) => [f.properties.id, f.geometry.coordinates]))]);
              }
              return out; }""",
            seconds * 10,
        )
        browser.close()
    ratios, fastest = [], []
    for vid in set(samples[0][1]) & set(samples[-1][1]):
        speeds = [
            metres(a[vid], b[vid]) / ((t1 - t0) / 1000)
            for (t0, a), (t1, b) in zip(samples, samples[1:], strict=False)
            if vid in a and vid in b
        ]
        moving = [s for s in speeds if s > 0.5]
        if len(moving) > len(speeds) / 3:
            ratios.append(max(moving) / statistics.median(moving))
            fastest.append(max(speeds))
    ratios.sort()
    fastest.sort()
    pick = lambda xs, f: round(xs[int(len(xs) * f)], 2)  # noqa: E731
    print(
        f"{len(ratios)} moving vehicles: fastest/median speed p50 {pick(ratios, 0.5)}, "
        f"p90 {pick(ratios, 0.9)}; fastest m/s p50 {pick(fastest, 0.5)}, p90 {pick(fastest, 0.9)}"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("check", choices=["accuracy", "smoothness"])
    parser.add_argument("--url", default=SITE)
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    if args.check == "accuracy":
        accuracy(args.url.rstrip("/"), args.minutes)
    else:
        smoothness(args.url.rstrip("/"), args.seconds)


if __name__ == "__main__":
    main()
