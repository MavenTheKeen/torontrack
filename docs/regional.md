# Regional trains

The map shows the regional rail network: seven lines from Stratford, Kitchener and
Niagara Falls to Barrie and Oshawa, their 71 stations, every train that should be running
now, and each station's next departures.

Data used in this product or service is provided with the permission of Metrolinx.

## Source and licence

Metrolinx publishes static GTFS for its trains and buses at
`https://assets.metrolinx.com/raw/upload/Documents/Metrolinx/Open%20Data/GO-GTFS.zip`
(about 18 MB, updated every few weeks). Downloading it means accepting the Metrolinx
Access and Use Agreement, which requires the attribution line above and excludes
Metrolinx's trademarks from use with the data. So the site calls these regional trains,
and the loader drops the brand suffix from station names and destinations (the feed's
Union Station entry becomes "Union Station"). Line names and colours are data and are
kept. The airport rail link is a separate feed and is not loaded.

## Loading

`transit regional-refresh` downloads the zip and, when its SHA-256 is new, loads the
train subset (GTFS route type 2) as a new feed version in one transaction. The
`regional_schedule` Airflow DAG runs it daily at 09:10 UTC once unpaused; every attempt is
a row in `ops.regional_refreshes`. The October 2026 feed has 7 lines, 71 stations,
16,596 trips over 54 service days and 152,079 stop times, and loads in about 13 seconds.

| Table | Holds |
| --- | --- |
| `normalized.regional_versions` | Each loaded zip: SHA-256, Metrolinx's version, service date range |
| `normalized.regional_lines` | Code (`LW`), name and colour |
| `normalized.regional_stations` | Station ID, cleaned name and coordinates |
| `normalized.regional_shapes` | Track geometry per shape |
| `normalized.regional_service_dates` | Which service IDs run on which dates |
| `normalized.regional_trips`, `regional_stop_times` | Every trip with its timed stops |

Metrolinx lists stop times in descending order within a trip, so they are sorted by
sequence; trips with fewer than two timed stops are dropped, and a stop that is not in
`stops.txt` fails the load. Each refresh then deletes versions whose service has ended
(keeping the newest): nothing historical joins against this schedule, and Metrolinx
publishes a new one every few weeks at about 70 MB each.

## Realtime API terms

Metrolinx's realtime API has its own agreement, stricter than the schedule licence. Besides
excluding its trademarks, it forbids redistributing the data "within your own API, or other
feed", using the agency's name in promotion or suggesting an official app, and building
something that replicates the agency's own app or its look and feel. So when realtime data
is added: it feeds only this site's map, is left out of the open-data exports, is not
documented as a public API, and the access key stays server-side. The site remains a
multi-agency map with its own design, not a regional-rail app.

## Positions are from the timetable

Metrolinx's realtime API (trip updates, vehicle positions, alerts) needs a registered
access key. Until one is configured, trains are placed where the timetable says they
should be: at a station while it dwells there, and in between interpolated by time along
the track shape (stations are projected onto the shape in order, so lines that double back
stay right). A trip with no shape moves in a straight line between stations. Trips from
the previous service day are included, since late trains run past midnight.

Every regional response carries `"source": "timetable"`, and the map, train panel and
station panel all say so. Delays and cancellations do not show, so this is not a
reliability measure and is not used in any report.

## Endpoints

| Path | Returns |
| --- | --- |
| `/api/regional` | Lines with track paths (one shape per pair of end stations) and stations with their lines |
| `/api/regional/trains` | Every train that should be running now: line, destination, position, heading, next station and its scheduled time |
| `/api/regional/stations/{id}` | A station's lines and its next scheduled departures within three hours (12 at most); trains ending there are left out |

## Map coverage

Beyond Toronto, the street map includes the whole rail network at regional detail
(zoom 12 and below; see [street map](map.md)), so lines out to Kitchener or Barrie sit
on a basemap of towns, highways and water rather than a blank background.
