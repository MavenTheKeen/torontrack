# Street map

The site's map is drawn on a street basemap served by the site itself, so visitors'
browsers make no third-party requests. The basemap is a Toronto extract of
[Protomaps](https://protomaps.com/)' daily OpenStreetMap build, in one
[PMTiles](https://github.com/protomaps/PMTiles) file that the browser reads in ranges.

## Building it

```powershell
docker compose --profile tools run --rm map-tiles
docker compose restart web
```

`transit map-tiles` picks the newest Protomaps build, reads its directory and only the
tiles covering every subway station and Bike Share dock (zoom 0 to 15), plus the whole
regional rail network from Stratford to Bowmanville and Niagara Falls to Barrie at regional
detail (zoom 0 to 12), with HTTP range requests, merging neighbouring tiles into one request, and writes `toronto.pmtiles` to
the `map-data` volume under a temporary name before renaming it into place. A sidecar
`toronto.json` records the source build, tile count, size and SHA-256. On 2026-10-13 the
extract was 5,197 tiles and 86 MB (4,164 tiles and 68 MB for Toronto alone), built in
under 30 seconds with 67 requests. Rebuild every
few months to pick up map edits; `--build 20261013.pmtiles` pins a specific build.

The web service serves the volume read-only at `/map-data/`. Without the file the map
still shows subway lines, trains, stations and docks on a plain background, with a note
saying how to build it.

## Bike lanes

`transit map-tiles` also writes `bike-lanes.geojson` next to the basemap
(`transit bike-lanes` rebuilds only that): the City of Toronto's cycling network
(1,581 segments in 2026, updated about twice a year), simplified to geometry and four
rider-facing classes from the better side of each street: protected cycle tracks, painted
bike lanes, multi-use trails (dashed) and shared routes. The source marks empty fields
with a single space and some nulls with the HTML-escaped text `&lt;Null&gt;`; both count
as missing. The layer is 1.4 MB, served compressed, and off until it is switched on in the
layers panel.

## Station entrances, parking and bike parking

`transit map-tiles` also runs `transit osm-layers`, which reads BBBike's daily
OpenStreetMap extract of Toronto (the trip planner's street source, about 100 MB,
downloaded to a temporary file and deleted afterwards) and writes three layers next to the
basemap. To rebuild only these:

```powershell
docker compose --profile tools run --rm map-tiles osm-layers --out /srv/map
docker compose restart web
```

`--source` reads a local `.osm.pbf` instead of downloading one. The build takes about two
minutes and needs pyosmium's native library, which only the app image installs. The same
run writes `places.sqlite`, the trip planner's address and place search (120 MB;
[trip planner](planner.md#typing-a-start-or-end)).

| File | Contents | 2026-10-14 |
| --- | --- | --- |
| `entrances.geojson` | Subway station entrances (`railway=subway_entrance`): name, step-free (`wheelchair=yes`), not step-free (`wheelchair=no`), automatic (no collector) and exit only. | 280 entrances (68 KB); every one of the 70 stations has at least one |
| `parking.geojson` | Car parks open to the public, and the entrances mapped for them (`amenity=parking_entrance`): kind (surface, underground, multi-storey, rooftop), fee, capacity, height limit, hours, park and ride, operator. | 2,832 car parks, 857 entrances (0.8 MB) |
| `bike-parking.geojson` | Bicycle racks, shelters and lockers that are not private: kind, capacity, covered, fee. | 6,492 places, 29,843 mapped spaces (1.1 MB) |

`osm-layers.json` records the build time, source, file sizes and counts.

Each entrance is matched to the nearest named street within 80 m and the side of that
street it is on, from the perpendicular between the street and the entrance rounded to a
compass point, and the files carry it as text: "north side of Bloor St E", or "on Yonge St"
for a point within 4 m of the street line. A car park's centre says little about which
street it opens onto (a garage between two streets can be closer to the back one), so a car
park is described only as "near" its street, and the side is given for its mapped
entrances. An entrance joins the car park whose outline is within 40 m; one with no car
park nearby is listed as a car park of its own.

Car parks marked private, customers-only, permit, residents or staff, and on-street
parking, are left out, as are unnamed lots with no fee, operator, public access or
park-and-ride tag, which are mostly building lots. Car parks mapped as multipolygon
relations are skipped. OpenStreetMap has no live occupancy or rates; the car park panel
says so.

The web service assigns each subway entrance to the nearest station within 350 m and
counts bike parking within 250 m of each station; both are part of `/api/map` and are
recomputed only when the files or the schedule change. Station entrances appear from zoom
15, step-free ones in their own colour. Parking and bike parking start off; each file
loads the first time its layer is switched on.

## Moving vehicles

Between data refreshes (every 10 seconds), the map moves trains, buses and streetcars by
estimate, so they travel along their routes instead of jumping. The rules are in
`static/motion.js`, tested in `tests/test_motion.py`. Positions are recomputed on every
animation frame when zoomed in to street level (every 50 ms at zoom 12 to 14 and every
250 ms further out, where a frame's movement is under a pixel), and only vehicles in or
near the view are sent to the map; recomputing all 1,500 vehicles takes about 2 ms. When
fresh data moves an estimate, the difference is made up evenly over the 10 seconds until
the next refresh, so a vehicle drawn behind runs slightly faster throughout instead of
lurching forward after each refresh (a 2.5-second eased glide did that: drawn speed
peaked at about twice the usual). A vehicle drawn up to about 150 m ahead of its
estimate waits rather than backing up, and a correction over about 250 m (a detour, a
report long missing) glides in 1.2 seconds. Heading arrows turn at up to 180 degrees a
second instead of snapping at each corner of the route.

- **Subway trains** follow TTC's predicted arrival at the next station: a train runs from
  the previous station to the next over the scheduled run time, arriving at the predicted
  time, and waits there until a refresh says it has left. `/api/trains` gives each train's
  `from_index`, `next_index`, `arrives_at` and `segment_seconds`.
- **Buses and streetcars** start from their last GPS report and move along their route's
  stop pattern at the route's usual pace for the hour. A vehicle that reported standing
  still is held for 20 seconds first. Only the movement is added to the reported position,
  which keeps the report's own offset from the straight lines between stops. Movement
  stops 2 minutes after the report, and the vehicle fades once its report is 2.5 minutes
  old. `/api/vehicles` gives each vehicle's `pattern`, fractional stop `position`,
  `reported` time, `stopped` and `pace`; `/api/patterns` (about 200 KB compressed, loaded
  once) gives each pattern's stops with their timetable times.
- **Regional trains** follow their timetable: `/api/regional/trains` gives each train's
  `track`, its position every 10 seconds for the next 2 minutes.

A route's usual pace is the timetable seconds its vehicles cover per real second at that
hour, measured from consecutive stop passages of the same vehicle over the last 28 days
(the dbt model `analytics.ttc_surface_pace`, rebuilt with the other models every 15
minutes and read by the site hourly; about 5,500 route, direction and hour combinations).
Over 28 days of passages the calculation takes several seconds, so it is not run while a
visitor waits. The 504 King streetcar, for example, runs at 0.75 to 0.8 of its timetable
pace in the rush hours and faster than it overnight. Without passages, the timetable's pace
is used.

`/api/vehicles` says which pace each vehicle has in `pace_source` (`usual` or
`timetable`). Moving vehicles at the pace that meets TTC's prediction for a stop ahead
was tried on 2026-10-16 and measured worse: over 10 minutes, a median error of 87 m at
the next report against 45 m for the usual pace, so it is not used.

`scripts/motion_check.py` measures both sides of this against the live site: `accuracy`
compares where the map would place each vehicle when its next report arrives with that
report, by pace source; `smoothness` samples the drawn vehicles in a headless browser and
reports how uneven their speed is.

### How the bus and streetcar estimate was chosen

On 2026-10-14, TTC's vehicle feed was recorded every 10 seconds for 45 minutes in the
evening rush (154,227 reports from 1,841 vehicles). From each report, each estimator
predicted where the vehicle would be at each later report from it up to 150 seconds on,
about 630,000 comparisons, and the error is the straight-line distance to that later
report (which includes GPS error of 5 to 15 m). Median and 90th-percentile errors:

| Time since the report | Report as-is (before) | Usual pace, 20 s hold when stopped (chosen) |
| --- | --- | --- |
| 20 to 40 s | 59 m, 271 m | 53 m, 175 m |
| 40 to 60 s | 174 m, 437 m | 97 m, 256 m |
| 60 to 90 s | 186 m, 543 m | 118 m, 312 m |
| 90 to 150 s | 329 m, 809 m | 160 m, 443 m |

Reports were typically 35 to 65 seconds old when shown, so this roughly halves the
error at the delays visitors see. Moving the matched point on the pattern instead of the
report (worse: the pattern is straight lines between stops), each vehicle's own recent
pace (no better at the median, worse at the 90th percentile), and holding stopped
vehicles for 10 or 30 seconds were also tested. 55% of reports said the vehicle was
standing still.

## Style

The basemap style is defined in the page (`mapStyle` in `app.js`) rather than taken from a
theme, with light and dark variants that follow the visitor's setting: muted land, water,
parks, roads and buildings, and small neighbourhood and street labels, so the subway lines,
trains and docks carry the colour. It uses the Protomaps v4 layer names (`earth`, `water`,
`landuse`, `roads`, `buildings`, `places`).

## Licence

Map data © OpenStreetMap contributors, available under the
[Open Database License](https://www.openstreetmap.org/copyright). Basemap tiles by
Protomaps.
