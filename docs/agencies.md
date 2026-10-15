# Neighbouring bus agencies

The map shows Brampton Transit and MiWay (Mississauga) buses beside the TTC's: live
positions, stops, routes and estimated arrivals. York Region Transit is to follow once
its feed access is granted.

## Sources and licences

| Agency | Static GTFS | Live positions | Licence |
| --- | --- | --- | --- |
| Brampton Transit | City of Brampton open data (ArcGIS item `a355aabd…`) | GTFS-Realtime vehicle positions, `gtfs-rt-merge.prod.bt-cadavl.com` | CC BY 4.0: credit required |
| MiWay | `www.miapp.ca/GTFS/google_transit.zip` | GTFS-Realtime vehicle positions, `www.miapp.ca/GTFS_RT` | City of Mississauga Terms of Use: redistributed data must carry the terms' URL |

Both licences forbid implying endorsement, and neither covers the agencies' logos or
trademarks. The site uses its own colours (teal for Brampton Transit, indigo for MiWay)
and plain names, credits both in the page footer, the map and every bus endpoint's
`attribution`, and states that it is not affiliated with either.

Measured on 2026-10-15: Brampton Transit 70 routes, 2,846 stops, 247 stop patterns;
MiWay 67 routes, 3,137 stops, 215 patterns; about 290 and 350 buses reporting at midday.

## How it works

- **Network** (`transit agencies-refresh`, daily `agency_schedules` DAG): each agency's
  zip is parsed like the TTC's surface network (`transit.ttc.surface.parse`): routes,
  stops, and stop patterns with average scheduled times. A zip is loaded only when its
  SHA-256 changed; the new version replaces the agency's previous one.
- **Live positions** (`transit agencies-collect`, the `agencies-collector` service):
  every 15 seconds each agency's vehicle positions are fetched and each bus's latest
  report is kept in `normalized.agency_vehicles`; reports over an hour old are deleted.
  The latest poll per agency is in `ops.agency_polls`, which `transit agencies-health`
  (the container health check) reads.
- **Site**: `/api/surface`, `/api/vehicles`, `/api/patterns`, `/api/stops/{id}` and
  `/api/routes/{id}` return the agencies' data with the TTC's. IDs are prefixed with the
  agency's key (`br:511`, `mi:9`) because route and stop numbers repeat across agencies;
  a route's `mode` is its agency's key. Buses are matched to their route's patterns by
  geometry and moved between reports exactly as TTC buses are ([map](map.md)), at the
  timetable's pace, since no stop-passage history is kept for them.
- **Destinations**: headsigns are reduced to a destination (`transit.agencies.towards`):
  "71 W Sheridan To Plymouth Loop" becomes "Plymouth Loop", "Queen Eb-Fccc" becomes
  "Queen East", and the regional rail brand is dropped from station names as elsewhere.

Arrival times at these stops are estimates from each bus's position and the schedule,
as for TTC buses. Both agencies also publish trip updates (their own predictions), which
are not used yet.

## Not yet covered

- **Trip planner**: the planner's street data ends at Toronto's boundary, so planning
  through Brampton and Mississauga needs a larger OpenStreetMap extract; the agencies'
  schedules will be added with it.
- **York Region Transit**: its feeds are released after accepting its Open Data Licence
  through an online form.
