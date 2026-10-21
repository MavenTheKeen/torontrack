# Regional trains

The map shows the regional rail network: seven lines from Stratford, Kitchener and
Niagara Falls to Barrie and Oshawa, their 71 stations, every running train with its live
position and delay, each station's next departures, and service alerts.

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
something that replicates the agency's own app or its look and feel. So the realtime data
feeds only this site's map, is left out of the open-data exports, is not documented as a
public API, and the access key stays server-side (a Compose secret, never in a URL a
browser sees). Alert text is shown without the trademarks (`regional_live.unmark`): "Old
Cummer GO - Construction" becomes "Old Cummer - Construction". The site remains a
multi-agency map with its own design, not a regional-rail app.

## Live trains

With a Metrolinx API key in `secrets/metrolinx_api_key`, the agency collector also polls
Metrolinx's GTFS-Realtime feeds at `api.openmetrolinx.com` (`transit.regional_live`):
vehicle positions and trip updates every 30 seconds, alerts every 2 minutes. It asks for
protobuf, about a sixth of the size of the JSON the API sends by default (42 KB against
241 KB for positions). The feeds cover GO buses too; only trips on the loaded rail lines
are kept, about 40 trains on a weekday afternoon. Their trip IDs are the timetable's own,
so each live report joins its trip directly.

| Table | Holds |
| --- | --- |
| `normalized.regional_live_trains` | Each running train's latest GPS fix, its delay at the next station, and whether it is cancelled |
| `normalized.regional_live_stops` | Predicted departure at each station ahead, and stations it will skip |
| `normalized.regional_alerts` | Alerts naming a rail line or station, without the bus ones |
| `ops.regional_polls` | Each feed's latest poll, its last success and any error |

Only the latest state is kept, in unlogged tables like the other live feeds. A failed
poll keeps the previous state; the site ignores reports older than 3 minutes. Trains no
longer in either feed are removed after 10 minutes.

### Placing a train

Every train is placed along its trip's track by the timetable, as before the key was
added: at a station while it dwells there, in between interpolated by time along the track
shape (stations are projected onto the shape in order, so lines that double back stay
right), and in a straight line between stations for a trip with no shape. Trips from the
previous service day are included, since late trains run past midnight. Live data changes
the time the timetable is read at:

1. A GPS fix under 3 minutes old is projected onto the trip's track; the train is placed
   where the timetable has it at that point, then moved on at the timetable's pace for
   the age of the fix. A fix more than 1 km from the track is ignored.
2. Otherwise the trip update's delay is used: a train 5 minutes late is placed where the
   timetable had it 5 minutes ago.
3. Otherwise the timetable alone.

On 21 October 2026 all 40 live GPS fixes were within 1 km of their own track. Placing by
the delay alone would have put trains a median of 229 m (90th percentile 921 m) from
their fix, so the fix is preferred; the delay implied by the fix was within about 20
seconds of Metrolinx's own.

Cancelled trains are not shown on the map and are marked in station departures. Each
train and departure says whether it is live and by how many minutes it is late or early;
responses carry `"source": "live"` when any train is live and `"timetable"` otherwise.
These delays are shown as they are reported; they are not stored over time, so they are
not used in any reliability report.

The pipeline page shows the feeds' health, and the agency collector's health check fails
when positions or trip updates have not loaded for 2 minutes, or alerts for 10.

## Endpoints

| Path | Returns |
| --- | --- |
| `/api/regional` | Lines with track paths (one shape per pair of end stations) and stations with their lines |
| `/api/regional/trains` | Every running train: line, destination, position, heading, next station and its expected time, whether it is live and its delay; and the line alerts in effect |
| `/api/regional/stations/{id}` | A station's lines, its alerts, and its next departures within three hours (12 at most) with expected and scheduled times, delay and cancellations; trains ending there are left out |

These endpoints serve this site's map only; see the realtime terms above.

## Map coverage

Beyond Toronto, the street map includes the whole rail network at regional detail
(zoom 12 and below; see [street map](map.md)), so lines out to Kitchener or Barrie sit
on a basemap of towns, highways and water rather than a blank background.
