# What the site does

This page walks through the public site at <https://torontrack.skynetlab.duckdns.org>.
How each part is built is covered in the linked documents.

## Map

The map shows the subway with estimated train positions, every TTC bus and streetcar with
its route and heading, and more than 9,000 surface stops with arrival estimates. Brampton
Transit, MiWay and York Region Transit (including Viva) are shown the same way, so the map
runs from Mississauga to Keswick ([neighbouring agencies](agencies.md)).

Lines 5 Eglinton and 6 Finch West are drawn as lines, but the TTC publishes no live data
for them yet, so their trains are placed by the timetable. Regional trains between
Kitchener and Oshawa are also placed by the timetable, because Metrolinx's realtime feed
needs a key this project does not have yet ([regional trains](regional.md)).

Between data refreshes, vehicles keep moving: subway trains run toward their predicted
arrival at the next station, and buses and streetcars move along their route at that
route's usual pace for the hour. Checked against 630,000 later position reports, this
roughly halves the error of drawing each vehicle where it last reported
([map](map.md)). Zoomed out, trains become small dots and then disappear, and hidden
layers are not animated, which keeps the page light on a phone.

Close up, every subway station shows its entrances, which side of the street each is on,
and whether it is step-free. Optional layers add car parks and bike parking. Bike Share
docks show live bikes and free docks.

Location is optional. When it is switched on, the map lists the nearest stations, stops
and docks; the position stays in the browser and is never sent to the server.

The basemap is built from OpenStreetMap and served by the application itself, so the
browser makes no requests to a third-party map service.

| Trip planner | Station entrances |
| --- | --- |
| ![Directions from Casa Loma to 100 Queen St W, typed as a place and an address, with the travel modes to use](screenshots/site-planner.png) | ![Spadina station's entrances, each with its side of the street and whether it is step-free](screenshots/site-entrances.png) |

## Trip planner

Directions combine subway, streetcar, bus, regional train, Bike Share and walking across
Toronto, Brampton, Mississauga and York Region. Routing comes from a self-hosted
OpenTripPlanner built from five schedules (TTC, GO and UP Express, Brampton Transit,
MiWay, YRT) and an OpenStreetMap extract of the Golden Horseshoe. Plans use published
timetables, not live predictions ([trip planner](planner.md)).

The start and end can be typed as an address, a place, an intersection, a station or a
stop. The search index is built from OpenStreetMap and served by the site, so planning
does not need the visitor's location. Each mode of travel can be switched off. Subway
rides name the entrance to use and the exit to take. Step-free trips use only accessible
stations, and a trip through a station with an elevator out of service carries the TTC's
alert.

A planned trip can be followed step by step: walking turns, where to board, a live
arrival estimate while waiting for a TTC bus or streetcar, and the stops left on each
ride ([trip guidance](navigation.md)). The site can be installed to a phone's home screen.

| Trip guidance | Where Bike Share trips go |
| --- | --- |
| ![Guidance on Line 2: get off at St George, four stops left](screenshots/site-guidance.png) | ![A Bike Share dock's trips by hour and its top destinations, drawn as lines](screenshots/site-bike-trips.png) |

## Service status

The status page lists current TTC disruptions, a diagram of each subway line with
estimated train positions, and arrivals at every station. When trains stop appearing at
consecutive platforms for much longer than the scheduled interval, the line is marked as
a possible delay, labelled as not confirmed by the TTC.

## Reliability

The reliability page shows today's worst delays on each line. Long gaps between trains
that overlap in time count as one delay, ranked by how many minutes they ran over the
scheduled gap; a 12-minute gap where trains are scheduled every 12 minutes is not a
delay. An hourly table compares each line with its schedule today and over the last week.

The long-term report measures excess wait time: how much longer riders wait than the
schedule implies, by line, time of day, platform and weather. It also covers bus
bunching, bus service on days with and without road closures, and elevator outage
durations. TTC's official delay log is loaded for comparison
([reliability](reliability.md), [metric definitions](metrics.md)).

| Reliability report |
| --- |
| ![Extra wait by line and time of day](screenshots/site-report.png) |

## Bike Share

Each dock shows live availability and a summary of its trips over the last twelve months:
departures by hour and its most common destinations, drawn on the map. The summaries
cover 21.5 million trips since January 2024 ([Bike Share](bikeshare.md)). The city's
cycling network is available as a map layer.

## Events and road restrictions

The events page lists the next two weeks of city festivals and community events, and
concerts, games and shows from Ticketmaster, each with the nearest station and walking
time. Large arena and stadium events are flagged because they crowd nearby stations
([events](events.md)).

City construction zones and road closures are drawn on the map with the bus and streetcar
routes they affect ([road restrictions](roads.md)).

## Pipeline status

The pipeline page shows the health of every feed and job: when each last succeeded, how
many runs it completed against how many were expected, how old the newest data is, and
how many records validation rejected or flagged in the last hour, with the reasons
([site and API](site.md)).

## Open data

Observed train gaps and Bike Share dock readings are published daily as Parquet and CSV
files with checksums and column descriptions ([open data](datasets.md)).

| Service status | Station panel | Bike Share dock |
| --- | --- | --- |
| ![Subway status on a phone](screenshots/phone-status.png) | ![Union station: Line 1 running here with a closure elsewhere on the line, and its entrances](screenshots/phone-station.png) | ![Dock with live bikes and trip history](screenshots/phone-dock.png) |
