# TTC subway collection

The `ttc-collector` service polls TTC's public GTFS-Realtime feeds and stores subway
train predictions, bus and streetcar positions and service alerts in PostgreSQL. Subway
predictions and vehicle positions are polled every 10 seconds (`TTC_FAST_POLL_SECONDS`),
alerts every 30 (`TTC_POLL_SECONDS`).

The faster interval follows from measurements on 2026-10-14. Subway predictions change
every second. TTC publishes a new set of vehicle positions every 10 to 30 seconds, and
each report is already about 34 seconds old when it first appears, so a 30-second poll
added up to 30 more seconds of delay (15 on average) and a 10-second poll adds up to 10.
Alerts did not change within a minute. One poll of all four feeds took 0.8 seconds
(95th percentile about 1 second). A feed whose content has not changed is not stored
again, so vehicle storage follows TTC's own updates rather than the polling rate. Static GTFS (stops,
routes, trips, stop times) maps platform IDs to stations and line order.

Data: Toronto Transit Commission. Predictions may be inaccurate.

## Sources

| Feed | URL | Use |
| --- | --- | --- |
| Subway trip updates | `https://gtfsrt.ttc.ca/trips/subway?format=binary` | Predicted arrivals for the next few platforms per train |
| Subway alerts | `https://gtfsrt.ttc.ca/alerts/subway?format=binary` | Delays, closures, planned track work |
| Accessibility alerts | `https://gtfsrt.ttc.ca/alerts/accessibility?format=binary` | Elevator and escalator outages |
| Static GTFS | Toronto Open Data, "TTC Routes and Schedules" (ZIP) | Platforms, line order, scheduled trips |

The realtime feeds need no API key and publish no rate limits or terms of use. One
backend process polls them; browsers never call TTC directly. Only subway routes
(GTFS `route_type` 1: Lines 1, 2 and 4) are loaded. Lines 5 and 6 are light rail and
are not in the subway realtime feed.

## Data flow

1. **Raw**: each response is stored as received in `raw.ttc_realtime_snapshots`
   (protobuf bytes, SHA-256, fetch time). The feed's header timestamp is unique per
   feed, so polling the same publication twice stores it once.
2. **Validate and normalize**: in a separate transaction, the snapshot is decoded,
   validated against the active static GTFS version, and written to normalized tables.
   `transit ttc-replay` rebuilds normalized rows from stored raw snapshots without
   any HTTP request.
3. **Ops**: every poll is a row in `ops.ttc_poll_runs`; every rejected or flagged record
   is a row in `ops.ttc_record_issues`; static refreshes are in `ops.ttc_static_refreshes`.

| Table | Contents |
| --- | --- |
| `normalized.ttc_gtfs_versions` and `ttc_routes`, `ttc_stops`, `ttc_line_stops`, `ttc_trips`, `ttc_stop_times`, `ttc_calendar*` | Static GTFS keyed by `feed_version` (the zip's SHA-256). A new schedule is a new version; older versions stay for historical joins. |
| `normalized.ttc_current_predictions` | Predictions from the newest trip snapshot only. |
| `normalized.ttc_train_stop_events` | One row per train visit to a platform: latest predicted arrival, first/last seen, and `passed_at`. |
| `normalized.ttc_alerts`, `ttc_alert_versions` | Every distinct version of every alert, with first and last seen times. |

## Train identity and arrivals

The subway feed has no vehicle positions, only predicted arrival times for the next few
platforms of each train. Two findings from the live feed shape the model:

- **`trip_id` is not a stable train identity.** The same train usually gets a different
  `trip_id` on consecutive polls. The vehicle label is stable and unique per snapshot,
  so visits are keyed by train (vehicle label), with the latest `trip_id` kept for
  reference. A prediction for the same train and platform within 10 minutes of an
  existing visit updates that visit; a train passes a given platform at most once per
  round trip.
- **Arrival is inferred.** A visit is marked `passed_at` at the first snapshot that no
  longer lists that platform, provided the train was due there within 2 minutes when
  last listed (and was listed in the last 10 minutes). Its last `predicted_arrival` is
  the best available estimate of when the train was there. This holds whether or not
  the train is still in the feed: trains leave the feed at terminals and when their
  label changes. A platform dropped well before its predicted time is a withdrawn
  prediction (short turn, reroute), not an arrival. If the platform is listed again
  later, the mark is withdrawn.
- **The feed sometimes drops out.** About 1 in 10 trip snapshots on the first day of
  collection listed no trains at all (once, about half), between two complete ones.
  Accepting them would blank the live site and mark every train due within 2 minutes as
  arrived. Such a snapshot is skipped: its raw payload is kept and a `feed_dropout` issue
  is logged, but visits and current predictions are left alone. If the feed stays empty,
  no train was seen in the last 2 minutes, so the empty snapshot is accepted; the end of
  service and a real shutdown still show up within about 2 minutes.

Terminal platforms need care in analysis: trains dwell and change ends there, and the
vehicle label can change when they do. Near Wilson Yard, where trains enter and leave
service, one train is occasionally reported under two labels for a few minutes, which
shows up as two arrivals under a minute apart; these are flagged `implausible` and
excluded from reliability metrics.

## Validation

| Check | Action |
| --- | --- |
| Header missing version or timestamp, non-`FULL_DATASET`, undecodable protobuf | Snapshot fails; poll recorded as failed |
| Feed timestamp more than 5 minutes from fetch time | Flagged |
| Trip snapshot lists fewer than half the trains seen in the previous 2 minutes | Snapshot skipped (`feed_dropout`); raw kept |
| Missing or duplicate `trip_id`; duplicate vehicle in one snapshot | Rejected |
| Missing vehicle label | Flagged; the trip ID is used as the train ID |
| Platform not in static GTFS, or belonging to another line | Rejected |
| No arrival/departure time; time 30+ minutes past or 3+ hours ahead of the feed | Rejected |
| Platforms from both directions in one trip | Minority direction rejected |
| Same platform twice in one trip | Duplicate rejected |
| `stop_sequence` out of order; feed order differs from line order | Flagged; rows re-sorted into line order |
| Identical arrival time at consecutive platforms; decreasing times | Flagged |
| Alert with no English text, or duplicate alert ID | Rejected |
| Alert `effect` contradicts its text | Flagged (`alert_effect_text_mismatch`) |

## Alert status

Alerts have no severity field, and `effect` can contradict the text. For example, one
planned closure had effect `REDUCED_SERVICE` while its text said there was no subway
service. Each alert version gets:

- `effect_status` from the GTFS-Realtime effect,
- `text_status` from keywords in the text (no service, reduced service, delays,
  elevator/escalator),
- `derived_status`, the more severe of the two, in the order `no_service`,
  `reduced_service`, `delays`, `modified_service`, `accessibility`, `other`.

Accessibility alerts list platforms only; their lines are derived from static GTFS.
Alerts whose active period starts in the future are planned disruptions.

`advance_notice` marks notices of planned work worded in the future tense ("There will
be no subway service ... nightly ..."). TTC gives these an active period covering the
whole notice week, not the closure hours, so they must not be read as a current
disruption. Derived fields are recomputed on replay, so rule changes apply to history.

## Official delay log

TTC publishes its own subway incident log on Toronto Open Data ("TTC Subway Delay Data",
CSV, one row per incident: date, local time, station, delay code, minutes of delay,
minutes of gap between trains, direction, line). It is updated monthly, a few weeks after
each month ends. `transit ttc-delays-refresh` (daily in Airflow, `ttc_official_delays`)
resolves the current download from the catalogue, stores each distinct download in
`raw.ttc_delay_files`, and loads it into `normalized.ttc_official_delays` and
`ttc_delay_codes` only when it changed; `--reload` re-normalizes the stored file. Every
attempt is a row in `ops.ttc_delay_refreshes`.

The station field is free text. Matching to station keys handles what the log actually
contains: names cut at 22 characters (`PIONEER VILLAGE STATIO`), line suffixes
(`KENNEDY BD STATION`), old names (`DUNDAS` is TMU, `EGLINTON WEST` is Cedarvale),
abbreviations (`VMC`, `NORTH YORK CTR`), platform and approach wording, and sections
(`UNION STATION TO KING`, matched to the first station and marked `range`). Yards and
storage tracks stay unmatched. On the log as of 2026-09 (45,475 incidents since 2025),
99.4% of incidents with a gap between trains match a station; each row records how it
matched (`exact`, `alias`, `prefix`, `range`). The published code descriptions contain
double-encoded UTF-8 (`â€"` for a dash), which is repaired on load.

## Buses and streetcars

TTC's `vehicles/position` feed lists every bus and streetcar every 10 to 30 seconds:
position, heading, speed and how full it is (about 1,600 in service on a weekday
afternoon). The collector polls it with the other feeds and keeps only the latest position
of each vehicle in revenue service (`normalized.ttc_vehicles`); vehicles with no route are
out of service and are left out. Positions outside the Toronto area or more than 10
minutes old are rejected, and, as with subway trips, a snapshot listing fewer than half of
the vehicles currently shown is treated as a feed dropout and skipped. Position history is
not kept: at this rate it would add about 400 MB a day.

The surface network comes from the same static GTFS version as the subway (`ttc-gtfs-refresh`
loads it, also for a version loaded before buses were added): 233 routes (213 bus,
20 streetcar and light rail), 9,261 stops and 1,356 stop patterns in the October 2026
schedule. A pattern is one exact stop sequence on a route and direction, with the mean
scheduled time from its first stop to each stop across every trip using it. The 5-million-row
`stop_times.txt` is streamed trip by trip; only running sums per pattern are kept.

TTC publishes no arrival predictions for surface routes, and neither the feed's trip IDs nor
its stop IDs match the published schedule (a 504 streetcar on King St reported a stop the
schedule places on Coxwell Ave). So the site places each vehicle by geometry: it snaps to
the nearest point on one of its route's patterns, skipping segments running against its
heading, and is unmatched if more than 120 m from all of them. On 2026-10-13, 96% of
vehicles matched. An arrival estimate at a stop is the scheduled time along the matched
pattern from the vehicle's point to that stop, less the age of the position report; the
site labels these as estimates.

### Stop passages

Positions alone show where vehicles are, not how evenly they run. After each vehicle
snapshot the collector places every vehicle on its pattern again and compares that with
its previous placement (`ops.ttc_vehicle_progress`): each measurement stop passed in
between becomes a row in `normalized.ttc_surface_passages`, timed by linear interpolation
between the two position reports (vehicles report every 20 to 75 seconds, so a passage
time is good to about half that). Measurement stops are about one per 10 scheduled minutes
of each pattern (the middle stop on a shorter one), not all 9,000 stops, and never a
pattern's first or last stop: the first recorded evening showed buses queueing at
terminals a few seconds apart, which is not a wait anyone has. That is enough for route
headways at a fraction of the rows (about 100,000 a day, kept `TTC_EVENT_RETENTION_DAYS`). Progress
resets without recording when a vehicle changes pattern (a new trip), goes unmatched,
reports after more than 4 minutes, or jumps more than 12 stops; a step back of under one
stop is GPS noise and keeps the furthest position, so no stop is counted twice.

For comparison, the schedule loader also counts scheduled trips per pattern, service ID
and hour of first departure (`normalized.ttc_surface_pattern_starts`; 38,336 rows covering
all 131,793 trips in the October 2026 schedule).

| Table | Holds |
| --- | --- |
| `normalized.ttc_surface_routes` | Route number, name and mode (`bus`, or `streetcar` for GTFS route type 0, which includes Lines 5 and 6) per feed version |
| `normalized.ttc_surface_stops` | Stop ID, public stop code, name and coordinates |
| `normalized.ttc_surface_patterns` | Route, direction, destination and trip count of each stop pattern |
| `normalized.ttc_surface_pattern_stops` | Each pattern's stops in order with mean scheduled offset |
| `normalized.ttc_vehicles` | Latest position of each vehicle in service |

## Retention

Hourly, the collector deletes raw snapshots older than `TTC_RAW_RETENTION_DAYS`
(default 3; vehicle snapshots, ~130 KB each, after `TTC_VEHICLE_RAW_RETENTION_HOURS`,
default 6), train visits older than `TTC_EVENT_RETENTION_DAYS` (default 90), and poll
runs older than 30 days. The snapshot each feed currently points at is never deleted.
Alerts and static GTFS versions are kept. Once a day it rebuilds the train visit indexes
(`REINDEX ... CONCURRENTLY`, which does not block polling): each poll updates the predicted
arrival of every visit still in the feed, and after four days those updates had left the
indexes at 77 MB against 17 MB freshly built.

## Commands

```powershell
docker compose up -d ttc-collector              # polls every 10/30 s; loads static GTFS first if absent
docker compose run --rm ttc-collector ttc-gtfs-refresh   # load a new static version if changed
docker compose run --rm ttc-collector ttc-replay --pending
docker compose run --rm ttc-collector ttc-replay --snapshot-id 123
docker compose run --rm ttc-collector ttc-retention
docker compose run --rm --no-deps ttc-collector ttc-smoke   # live parse check, no writes
```

The `ttc_static_gtfs` Airflow DAG runs `ttc-gtfs-refresh` daily at 09:00 UTC once
unpaused. Only one collector can run per database (PostgreSQL advisory lock). The
container health check (`transit ttc-health`) fails unless every feed had a
successful poll in the last 2 minutes.
