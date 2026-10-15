# Neighbouring bus agencies

The map shows Brampton Transit, MiWay (Mississauga) and York Region Transit (including
Viva) buses beside the TTC's: live positions, stops, routes and estimated arrivals.

## Sources and licences

| Agency | Static GTFS | Live positions | Licence |
| --- | --- | --- | --- |
| Brampton Transit | City of Brampton open data (ArcGIS item `a355aabd…`) | GTFS-Realtime vehicle positions, `gtfs-rt-merge.prod.bt-cadavl.com` | CC BY 4.0: credit required |
| MiWay | `www.miapp.ca/GTFS/google_transit.zip` | GTFS-Realtime vehicle positions, `www.miapp.ca/GTFS_RT` | City of Mississauga Terms of Use: redistributed data must carry the terms' URL |
| York Region Transit | `www.yrt.ca/google/google_transit.zip` | GTFS-Realtime vehicle positions, `rtu.york.ca/gtfsrealtime` | YRT Open Data Licence: credit optional, in its prescribed wording; YRT may ask for the credit to be removed |

The licences forbid implying endorsement, and none covers the agencies' logos or
trademarks. The site uses its own colours (teal for Brampton Transit, indigo for MiWay,
magenta for York Region Transit) and plain names, credits each agency in the page footer,
the map and every bus endpoint's `attribution`, and states that it is not affiliated
with any of them. YRT's feeds are listed after accepting its licence through
`forms.yrt.ca/YRT-GTFS-Data`; the addresses themselves need no key.

Measured on 2026-10-15: Brampton Transit 70 routes, 2,846 stops, 247 stop patterns;
MiWay 67 routes, 3,137 stops, 215 patterns; about 290 and 350 buses reporting at midday.
York Region Transit's feed has 125 routes and 4,825 stops reaching north to Keswick;
about 316 buses were reporting in the afternoon. The map's street tiles cover the same
area (`transit.maptiles.BOUNDS`, north to latitude 44.34).

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
  agency's key (`br:511`, `mi:9`, `yr:601`) because route and stop numbers repeat across agencies;
  a route's `mode` is its agency's key. Buses are matched to their route's patterns by
  geometry and moved between reports exactly as TTC buses are ([map](map.md)), at the
  timetable's pace, since no stop-passage history is kept for them.
- **Destinations**: headsigns are reduced to a destination (`transit.agencies.towards`):
  "71 W Sheridan To Plymouth Loop" becomes "Plymouth Loop", "Queen Eb-Fccc" becomes
  "Queen East", "Newmarket Terminal - NB" becomes "Newmarket Terminal", and the regional
  rail brand is dropped from station names as elsewhere. Route numbers are shown as
  riders see them (`transit.agencies.route_number`): YRT's "001" becomes "1" and Viva's
  "blue" becomes "Blue".

Arrival times at these stops are estimates from each bus's position and the schedule,
as for TTC buses. The agencies also publish trip updates (their own predictions), which
are not used yet.

## Not yet covered

- **Trip planner**: the planner's street data ends at Toronto's boundary, so planning
  through Brampton, Mississauga and York Region needs a larger OpenStreetMap extract;
  the agencies' schedules will be added with it.
