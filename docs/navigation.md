# Trip guidance

After planning a trip ([trip planner](planner.md)), the map can guide it step by step:
walking turns, where to board, a live estimate while waiting for a TTC bus or streetcar,
stops left on each ride, and arrival. **Start** uses the device's location; **Preview**
replays the trip on its timetable ten times faster, for trying it on a computer.

## Design

Guidance is split into three parts so that the source of positions can change without
touching the rest:

| Part | File | Responsibility |
| --- | --- | --- |
| Location source | `static/guidance.js` | Reports positions through one interface |
| Guidance engine | `static/guidance.js` | Turns a trip and a position into the next instruction; no page or map code |
| Guidance screen | `static/app.js` | Draws the instruction, follows the traveller on the map, live waits, alerts |

### Location sources

A location source is an object with:

```js
source.kind                    // "gps" or "simulated"
source.start(onFix, onError)   // onFix({ lon, lat, accuracy, heading, speed, at })
source.stop()
```

`at` is the time of the fix in milliseconds. The engine compares positions with the
timetable using that time, so a simulated source can run its own clock.

Two sources exist:

- `browserLocationSource()` wraps `navigator.geolocation.watchPosition` with high
  accuracy. Browsers provide location only on secure origins (HTTPS or `localhost`).
- `simulatedLocationSource(trip, { speedup, intervalMs })` moves along each leg's path at
  its scheduled pace and waits at stops until the scheduled departure.

### Adding a location source

A native wrapper (for example Capacitor) can provide background location, which browsers
do not allow: when the screen is off or another app is open, a web page stops receiving
positions. To use it, implement the interface above around the native plugin and select
it in `startGuidance` (`static/app.js`). The engine and the screen need no changes. The
same applies to a fused location provider, or to a source that smooths raw GPS fixes.

### Engine

`newGuidance(trip)` measures each leg once and places its walking steps and passed stops
along its path. `updateGuidance(state, fix)` then:

1. projects the fix onto the current leg, giving distance along it and distance off it;
2. moves to the next leg when the end is within 25 m on foot, or 70 m on a ride once the
   traveller has boarded (120 m along the transit leg);
3. returns the phase (`walk`, `wait`, `ride`, `arrived`), an icon, a title, a detail line,
   the distance to the next turn on foot, and the stops left on a ride.

State only moves forward, so a noisy fix can delay the next instruction but never undo
progress. A walking fix farther than 50 m (or 1.5 times its accuracy, if larger) from the
route, three times in a row, marks the traveller as off route, and the screen offers to
re-plan from the current position.

Underground, GPS is unreliable. A subway ride counts as boarded once a fix places the
traveller 120 m along the line, and the stop count updates with each fix that arrives.

## Screen

- The map follows the traveller at street zoom and turns to the direction of travel.
  Panning stops following until **Recenter**.
- While waiting for a TTC bus or streetcar, the card shows the live estimate for that stop
  and route from `/api/stops/{id}`, refreshed every 30 seconds. Subway and regional legs
  show the scheduled time, and a subway leg names the entrance to use and, while riding,
  the exit to take ([trip planner](planner.md#entrances-and-exits)).
- On a Bike Share ride, the card says which station to ride to and the minutes left at
  cycling pace, and follows the route like a walk.
- The guidance bar has a safety button that opens the safety panel
  ([public site](site.md#map)).
- The phone vibrates before the last stop and on arrival, where the Vibration API is
  supported, and the Screen Wake Lock API keeps the screen on during guidance.

## Tests

`tests/test_guidance.py` loads `guidance.js` into a headless browser (Playwright) and
drives the engine with positions along a synthetic walk, bus ride and walk, checking the
order of instructions, stop countdown, that progress never goes back, off-route
detection, and that the simulated source follows the timetable. The tests are skipped
where Playwright's browser is not installed.

## Installing as an app

The site has a web app manifest and a service worker (`static/sw.js`), so it can be added
to a phone's home screen and opens full screen. The worker serves the page, scripts,
styles and map library network-first, falling back to the last copy when offline. It
never caches `/api/` responses or map tiles, so live data is never shown from a cache.
Installation requires HTTPS, so on a phone this is available once the site is public.
