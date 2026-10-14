# Subway reliability analytics

dbt models turn observed train arrivals into headway and outage metrics. They run in the
same `dbt build` as the Bike Share models (every 15 minutes from Airflow locally), and
the public site's Reliability page and `/api/reliability` read them.

Reliability numbers describe this project's observations, which start when collection
started. They are estimates from predicted arrival times, not official TTC statistics.

## Models

| Model | Type | Contents |
| --- | --- | --- |
| `staging.stg_ttc_platforms` | view | Platforms of the newest static GTFS version, with line order and a terminal flag. |
| `staging.stg_ttc_arrivals` | view | Train visits marked passed, with station and service hour (hours since local midnight of the service day; can exceed 23 after midnight, like GTFS). |
| `staging.stg_ttc_collection_gaps` | view | Periods over 2 minutes without a new trip-update snapshot. |
| `analytics.ttc_headways` | incremental | Time since the previous train at the same platform and service day, with `implausible` (under 60 s), `spans_collection_gap` and `outside_service` (a train passed more than 10 minutes before the platform's first scheduled train or 30 minutes after its last, such as a work train before opening, so the gap spans the overnight closure) flags. Keeps history after train visits expire; each run rebuilds the last two service days. |
| `analytics.ttc_scheduled_service` | incremental | Scheduled trains per platform and service hour on each observed day, using the static GTFS version whose calendar covers that day. |
| `analytics.ttc_headway_reliability_hourly` | table | Observed versus scheduled headways per line, direction, day and hour, with the day type, that hour's weather (`dry`, `rain` or `snow`; null until Environment Canada publishes the hour) and the sums for excess wait time. |
| `analytics.ttc_station_reliability` | table | The same measures per platform and day, for ranking platforms. |
| `analytics.ttc_collection_gaps` | incremental | Every collection gap, kept after poll runs expire, for the report's coverage. |
| `analytics.ttc_service_days` | incremental | Day type of each observed service day (`weekday`, `saturday`, `sunday`, `holiday`) from TTC's calendar: a date whose regular service a calendar exception removes runs a holiday schedule. |
| `analytics.ttc_longest_gaps` | table | Ten longest gaps per line and day. |
| `analytics.ttc_elevator_outages` | view | Elevator/escalator outages with durations. |

## Definitions

- **Measurable headway**: not at a terminal, not implausibly short, and not spanning a
  collection gap. Only these count in reliability metrics.
- **Regular headway**: at most 1.5 times the scheduled headway for that platform and hour.
- **Long gap**: more than twice the scheduled headway and more than the scheduled headway
  plus 5 minutes. The live site uses the same rule.
- **Longest gaps** are counted once per pair of consecutive trains. A held train shows the
  same gap at every platform downstream; the incident is reported at the platform where
  it was longest.
- **Excess wait time (EWT)**: how much longer than the schedule promises a rider who
  arrives at a random moment waits on average. With observed headways h and scheduled
  headways s, the average wait is sum(h²) / (2 sum(h)) and the scheduled wait is
  sum(h × s) / (2 sum(h)); EWT is the difference. Uneven gaps raise it even when trains
  run as often as scheduled, which an average headway misses; it is the measure
  Transport for London reports for frequent services. The models store the three sums so
  any hours, days, lines or platforms combine exactly. EWT below zero means waits were
  shorter than scheduled.
- **Outage duration** runs from when this project first saw the alert to when it left
  the feed (or now, if still active). Outages already present when collection began are
  flagged `began_before_collection`; their durations are lower bounds.

## Detected delays (live)

`/api/status` adds a **detected** status, labelled "Possible delay, not confirmed by TTC",
only when TTC reports nothing for the line. A line is flagged when at least two adjacent
non-terminal platforms in one direction have had no train for longer than the long-gap
threshold, no train is due within a minute, predictions are fresh, and there was no
collection gap since the last train. The message names the platform with the longest
wait, e.g. "No southbound train at Bloor-Yonge in 11 min".

## Checking against TTC's own log

`analytics.ttc_delay_detection` pairs each incident in TTC's [official delay
log](ttc.md#official-delay-log) with what this project observed. It takes incidents with
a gap between trains, at a matched station and line, while the collector was running, and
finds the longest observed gap at that station and line (and direction, when the log gives
one) overlapping the incident: from 5 minutes before it to 5 minutes after the reported
gap. An incident counts as `detected` when the observed gap is at least 75% of the gap TTC
reported; observed gaps are measured between inferred arrivals, so they rarely match to
the minute. Incidents during a collection gap are marked `in_coverage = false`.

This answers how many of TTC's own incidents this project's data would have caught. The
reverse question, long gaps this project saw that TTC did not log, needs care: many short
holds are never logged. The model is empty until TTC publishes a month this project
collected (collection began 2026-10-09; October should appear in late November).

## Buses and streetcars

Bus and streetcar headways come from stop passages recorded from live positions
([stop passages](ttc.md#stop-passages)), at measurement stops about every 10 scheduled
minutes along each pattern, never at a terminal.

| Model | Grain |
| --- | --- |
| `stg_ttc_surface_passages` | One passage with its service day (04:00 to 04:00) and hour |
| `stg_ttc_vehicle_gaps` | Over 2 minutes without a new vehicle snapshot |
| `ttc_surface_scheduled` (incremental) | Scheduled vehicles per route, direction, measured stop and hour, from trip starts per pattern placed at the stop by the pattern's scheduled offset (one hour off for some trips near the top of an hour) |
| `ttc_surface_headways` (incremental) | Time since the previous vehicle of the route and direction at the stop, with that hour's scheduled headway; the same vehicle twice in a row (a loop) and gaps spanning a collection gap are flagged |
| `ttc_surface_reliability_daily` | Per route, direction and day: headways, bunched, long gaps and the excess-wait sums |

Only frequent service counts: hours where the schedule has a vehicle at least every 15
minutes. On less frequent routes riders time their trips to the timetable, so the gap
between buses is not their wait. **Bunched** is a headway under a quarter of the scheduled
one (vehicles arriving together); a **long gap** uses the subway rule. Excess wait time
uses the same sums. The report ranks routes with at least 300 measured headways.

Passage times are interpolated between position reports 20 to 75 seconds apart, so a
headway is good to about half a minute: fine against a 5- to 15-minute schedule, too
coarse to call two vehicles exactly together.

Days with and without road restrictions along each route are compared in
`ttc_surface_closure_effect`; see [road restrictions](roads.md#closures-and-bus-reliability).

## Changing incremental models

`ttc_headways`, `ttc_scheduled_service`, `ttc_service_days` and `ttc_collection_gaps` are
incremental, so they keep history after the train visits (90 days) and poll runs
(30 days) they are built from expire. With `on_schema_change: fail`, a build stops
rather than silently mixing old and new columns. After changing their
columns, rebuild once with `transit transform --full-refresh` (recorded in
`ops.transformation_runs` like any build). A full refresh rebuilds headways from the
train visits still retained (90 days), so history older than that would be lost; past
that point, add the column with a migration-style `ALTER TABLE` and backfill instead.

## Tests

Structural tests (unique keys, not null, accepted values, relationships) and model
invariants (non-negative headways, consistent counts and percentiles) fail the build.
`ttc_implausible_headway_share` is a warning: it signals a change in TTC's feed without
failing the shared Bike Share build. `tests/test_ttc_dbt_integration.py` runs the real
dbt project on controlled arrivals, including a second incremental run.
