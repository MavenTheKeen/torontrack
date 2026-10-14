# Metric definitions

All historical metrics use actual collected GBFS station snapshots. No historical
rows are synthesized, and gaps are not filled. Counts do not imply exact empty/full
duration or trip activity.

## Time and freshness

| Timestamp | Meaning |
| --- | --- |
| `collected_at` | Actual status-response acquisition time recorded by this collector. |
| `status_fetched_at` | Fetch time of the preserved station-status payload. |
| `source_published_at` | GBFS envelope `last_updated`, reflecting publisher knowledge. |
| `station_reported_at` | Station `last_reported`, reflecting station communication. |

UTC is used internally. The dashboard displays `America/Toronto`. A scheduler's
logical date is not evidence of an observation at that time.

The project freshness policy allows report ages from **-300 through 1800 seconds**
inclusive. A report more than five minutes in the future or thirty minutes old is
ineligible. The future allowance tolerates clock skew without changing timestamps.
Thirty minutes is the project's explicit threshold, not a claim that GBFS defines
that value or that an old sample remains useful for a rider.

For historical metrics, source and station ages are measured against
`status_fetched_at`. For current availability, the dashboard checks status fetch,
source publication, and station report ages against the current time. A previously
valid historical observation can therefore remain eligible while the current
dashboard correctly marks the latest available reading stale.

## Counts, eligibility, and percentages

Each station metadata row in a succeeded collection is a **metadata snapshot**.
It is an **observed snapshot** only if that collection contains the station's
status row. Missing counts remain NULL, never zero.

| Metric | Definition |
| --- | --- |
| Rental-eligible snapshot | Observed, source/station fresh, installed, rentals enabled. |
| Return-eligible snapshot | Observed, source/station fresh, installed, returns enabled. |
| Empty snapshot | Rental eligible and available bikes = 0. |
| Full snapshot | Return eligible and available docks = 0. |
| Empty percentage | `100 × empty_snapshots / rental_eligible_snapshots`. |
| Full percentage | `100 × full_snapshots / return_eligible_snapshots`. |
| Mean available bikes | Bike-count sum across rental-eligible snapshots / their count. |
| Mean available docks | Dock-count sum across return-eligible snapshots / their count. |

Rental and return denominators are separate. A station accepting returns but not
rentals can enter the return denominator only. Inactive stations and stale,
future, or missing reports enter neither. Optional disabled-equipment counts and
capacity do not determine eligibility. Available bikes plus available docks are
not required to equal capacity.

A zero eligible denominator produces NULL: there is no percentage to rank. The
dashboard's minimum eligible-snapshot filter prevents an undefined or insufficiently
supported station from entering the selected ranking. It is a sample-size filter,
not a confidence interval.

For example, four empty observations among ten rental-eligible observations yield
40%, even if the selected period also contains two missing and three stale status
snapshots. Those five snapshots are not included as either empty or nonempty.
The underlying eligible and observed counts must accompany the percentage.

These are percentages of observations, not time-weighted percentages. Additional
manual collections with new IDs are legitimate samples and affect snapshot metrics.
If sampling is uneven, comparisons may be biased. An approximately regular
15-minute schedule reduces that issue without establishing exact duration.

## Two distinct coverage measures

**Station observation coverage** is `100 × observed_snapshots / metadata_snapshots`
within succeeded collections in the selected period. Stale and inactive status
rows still count as observed; eligibility is a separate measure. This coverage
detects a missing station status in an otherwise collected feed. It does not
detect a failed request, a missed collection, or a station absent from metadata.

**Collection-slot coverage** is a system-level measure:

1. Partition UTC into 15-minute slots.
2. Keep only complete slots entirely contained in the selected period. Exclude
   partial slots at both period boundaries.
3. Count slots containing at least one succeeded collection with at least one
   stored observation.
4. Divide occupied slots by complete expected slots.

The denominator spans the **entire selected period**, including time before this
project began collecting. It is not shortened to the first observed sample.
Two collections in one slot count as one occupied slot. Empty slots remain
uncovered. A failed collection does not count as covered; a succeeded collection
with stale source data does, because slot coverage measures collection presence,
not freshness or station completeness. Evaluate all three signals together.

For example, a rolling day from 10:07 UTC yesterday to 10:07 UTC today contains
95 complete 15-minute slots: 10:15 yesterday through 10:00 today. A collection
at 10:03 today is visible in the selected period's snapshot metrics but its
partially elapsed slot is excluded from the coverage denominator and numerator.

## Aggregation and display

Selected periods are rolling 1-, 7-, or 30-day intervals with an inclusive start
and exclusive end. Period selection uses elapsed UTC time; Toronto time is a
display and hour-of-day grouping choice.

`analytics.station_hourly` groups by UTC hour, preserving the two distinct UTC
hours during a Toronto autumn clock change. `analytics.station_summary` adds
hourly numerators and denominators before computing all-time percentages. It does
not average percentages from differently sampled hours.

The dashboard's selected-period rankings aggregate staging rows directly. Its
hour-of-day profile groups eligible observations by Toronto clock hour across
the selected dates; repeated local hours during the autumn change contribute
to the same hour-of-day bucket. That profile answers how sampled availability
varies by local clock hour, not how many hours or trips occurred.

All summaries can be traced through collection/station keys and raw payload IDs.
Small CI fixtures and artificially mutated integration scenarios are test inputs,
never dashboard history.
