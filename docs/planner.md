# Trip planner

The map's Directions button plans trips by TTC subway, streetcar and bus, regional train
and bus, Bike Share, and walking. Type the start and end (an address, a place, an
intersection, a station or a stop), pick them on the map, start from your location, or use
"Directions from here / to here" in a station, stop, event or search result panel. Sharing
your location is never needed. Up to four trips are
listed, earliest arrival first. The chosen trip is drawn on the map, with its steps listed
below it. Trips can leave now, leave at a time, or arrive by a time, up to 60 days ahead.

Plans come from published timetables, not live predictions. The station and stop panels
still show live times.

Trip plans: [OpenTripPlanner](https://www.opentripplanner.org/) 2.10 (LGPL), self-hosted,
with:

- TTC schedules (City of Toronto Open Data, Open Government Licence – Toronto);
- regional train and bus schedules: Data used in this product or service is provided with
  the permission of Metrolinx;
- streets and paths © OpenStreetMap contributors (ODbL), as
  [BBBike's](https://download.bbbike.org/osm/bbbike/Toronto/) daily Toronto extract;
- Bike Share Toronto stations, with live bikes and docks, from its public GBFS feed
  (version 2), read by the planner every minute.

## Area

The street extract covers longitude −79.79 to −79.0 and latitude 43.48 to 43.92: Oakville
to Ajax, and from Lake Ontario to about Major Mackenzie Drive. Trips must start and end
inside it (`planner.EXTENT`). Regional trains still run beyond it, but stations further
out have no streets to walk from. A larger area would need a bigger OpenStreetMap
extract, and the graph's memory grows with it.

## Running it

It is optional and runs as its own Compose profile:

```powershell
docker compose --profile planner up -d
```

1. `planner-data` downloads the three inputs (about 156 MB) into the `planner-data`
   volume, with the configuration files OpenTripPlanner reads. A file is replaced only
   when its content changed, and `manifest.json` is rewritten only then.
2. `planner` (`docker/planner.sh`) builds the routing graph whenever `manifest.json` is
   newer than `graph.obj`, then serves it. It checks every 10 minutes and stops serving
   while it rebuilds, so it never holds the build and the server in memory at once. If a
   build fails it keeps serving the previous graph.
3. The daily `planner_data` Airflow DAG (06:20 Toronto time) downloads the inputs again,
   so new TTC and regional timetables reach the planner the same day.

The graph keeps service from 7 days ago to 60 days ahead, so it has to be rebuilt within
two months. The daily DAG does this whenever a timetable changes.

The web app reads `PLANNER_URL` (Compose default `http://planner:8080`) and checks it
once a minute. Without a running planner, `/api/plan/status` reports
`"available": false`, the map hides the Directions button, and `/api/plan` returns 503.

### Measured on this data (October 2026)

| | |
| --- | --- |
| Graph build | about 4 minutes, 3.7 GB peak (`PLANNER_BUILD_JAVA_OPTS`, default `-Xmx4g`) |
| Serving | about 1.5 GB (`PLANNER_JAVA_OPTS`, default `-Xmx2g`) |
| Disk | 156 MB of inputs, 245 MB graph |
| A plan | 0.3–3 s |

So the planner needs about 4 GB of free memory while it rebuilds, on top of the rest of
the stack.

## Typing a start or end

The From and To boxes suggest, as you type, subway and regional stations and bus and
streetcar stops from the map's own data, then addresses, named places and intersections
from `/api/places`. That search is self-hosted: `transit osm-layers` builds it from the
same OpenStreetMap extract into `places.sqlite` (SQLite full-text search) next to the
street map, so no request goes to a third-party geocoding service. On 2026-10-14 it held
784,276 addresses, 65,425 named places (shops, restaurants, schools, parks, offices,
hospitals, attractions, named buildings and the airport) and 42,659 intersections, in
120 MB, and answered in under 70 ms.

- Street words are abbreviated in the index and in queries alike, so "100 Queen Street
  West" finds "100 Queen St W".
- "Queen and Spadina", "Queen & Spadina" and "Queen at Spadina" find the intersection.
- A query starting with a house number prefers addresses; one joining two streets prefers
  intersections; otherwise major destinations (malls, museums, hospitals, universities,
  stadiums, the airport) come before shops and offices sharing a word.
- Words matching the place's own name beat words matching only its neighbourhood, whole
  words beat partial ones (except the word still being typed), and among equals the
  results nearest the map's centre (or your location, if shared) come first.
- Each result shows its city, or the nearest neighbourhood, so the same address in two
  cities can be told apart.

The map's search box finds the same places; a result opens a panel with "Directions from
here / to here".

## Choosing how to travel

"Travel by" switches each way of travelling on or off: subway, streetcar, TTC bus, GO
train, GO bus and Bike Share. Walking is always allowed. Bike Share starts off; the rest
start on. The choices, and the step-free option, are kept in the browser's local storage on
that device only, and apply to every trip until changed. Changing one re-plans the trip.

The planner has one mode per kind of vehicle, so subway, streetcar and train map directly.
TTC and GO buses are both buses, so leaving out one of them excludes that network's bus
routes by ID; the route lists are read from the planner and cached for six hours. With
every transit mode off, the planner returns walking (and Bike Share) trips only.

With Bike Share on, a trip can walk to a station with a bike, ride to a station with an
open dock, and walk on, alone or before or after transit. The planner uses the live counts
it read last; they can change before you arrive. Rides over 30 minutes cost extra.

## Step-free trips

With "Step-free trips" ticked, the planner is asked for wheelchair-accessible routing
(`step_free=true`): OpenTripPlanner uses only stops the TTC schedule marks accessible
(`wheelchair_boarding`) and avoids stairs on mapped walking paths. On 2026-10-14, a trip
from Walmer Road to Front Street boarded at Spadina normally and at Dupont step-free,
because the schedule marks Spadina's platforms not accessible.

The schedule does not change when an elevator breaks, so the live elevator and escalator
alerts are checked after routing: a subway ride that starts or ends at a station with an
active outage carries its alert text, and step-free trips without any outage are listed
first. TTC sometimes tags an alert with another station's platforms as well (a Bloor-Yonge
escalator was also tagged with Sheppard-Yonge's), so when an alert's text starts with the
name of one of the matched stations, only that station keeps it; the map's outage badges
use the same rule.

## Entrances and exits

Each subway ride gets the station entrance to use and the exit to take, from the
OpenStreetMap entrances ([street map](map.md#station-entrances-parking-and-bike-parking)):
the one closest to the planner's walking route between 40 m and 400 m from the station.
The first 40 m are skipped because the walk starts at the platform, under the station.
Exit-only entrances are not suggested for getting in, and step-free trips consider only
step-free entrances. When a step-free trip finds none mapped at a station, the step says
so instead of guessing. A walk shorter than 120 m between a subway ride and the next ride
stays inside the station (a bus bay or another line), so no exit is suggested for it.
Guidance repeats the entrance while waiting and the exit while riding.

## API

`GET /api/plan?from=lat,lon&to=lat,lon[&at=2026-10-14T08:30][&arrive=true][&step_free=true][&modes=subway,bus]`

`at` without a UTC offset is Toronto time; it defaults to now. `modes` is a comma-separated
list from `subway`, `tram`, `bus` (TTC), `rail`, `regional_bus` and `bike_share`; without
it, every mode but Bike Share is used, and an empty list means walking only. Each
itinerary has `start`, `end`, `seconds`, `transfers`, `walk_metres` and `legs`. Each leg
has `mode` (`walk`, `bicycle`, `subway`, `tram`, `bus`, `rail`), `network` (`ttc`,
`regional`, or null for walking and cycling), `rented` (true for a Bike Share ride, whose
`from` and `to` are its stations), `route`, `route_name`, `color`, `headsign`, `from`,
`to`, `start`, `end`, `seconds`, `metres` and `path` (`[lon, lat]` pairs), plus:

- `from_point` and `to_point`, the leg's ends as `[lon, lat]`;
- `from_stop_id` and `to_stop_id`, the boarding and alighting stops' IDs in their
  network's GTFS; the first is used to look up live arrival estimates for TTC buses and
  streetcars;
- for TTC subway legs: `entrance` and `exit`, each with `name`, `where` (the street side),
  `point`, `step_free`, `automatic` and `exit_only`, present when a walk joins the ride and
  null when no suitable entrance is mapped; and `outages`, a list of `station` and its
  elevator and escalator `alerts`;
- `stops`, for transit legs: the stops passed before getting off, each with `name`,
  `point` and scheduled `arrival`;
- `steps`, for walking and cycling legs: turn-by-turn instructions, each with `action` (`depart`,
  `left`, `right`, `slightly_left`, `continue`, `enter_station` and so on, as named by
  OpenTripPlanner), `street` (null for unnamed paths and stairs), `metres` and `point`.

Each itinerary also has `outages`, the stations with outages on any of its rides. Without
the database, plans still return, without entrances, exits or outages.

Plans are routing searches, far costlier than the site's cached reads, so `/api/plan`
also has its own limit of `WEB_PLAN_RATE_LIMIT_PER_MINUTE` (default 20) per client
address.

`GET /api/places?q=text[&near=lat,lon]` returns up to eight `places`, each with `name`,
`detail` (city or neighbourhood), `kind` (`address`, `intersection`, or the place's type,
such as `museum`), `lat` and `lon`; `available` is false until `places.sqlite` is built.

Regional names drop the operator's brand, as elsewhere on the site: a headsign like
`Kennedy GO 09:01 - Union Station GO 09:20` becomes `Union Station`, and stop names lose
the `GO` suffix. TTC headsigns keep the part after "towards".
