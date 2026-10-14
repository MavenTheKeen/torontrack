# Open data

The site publishes this project's own collected data as daily files, linked from the
Reliability page and listed in `/data/index.json` (or `/api/datasets`).

| Dataset | Files | One row per |
| --- | --- | --- |
| `ttc-headways` | One per TTC service day (04:00 to 04:00 Toronto time) | Observed gap between consecutive trains at a platform |
| `bikeshare-availability` | One per Toronto calendar day | Station listed in a 15-minute collection, with its status if reported |
| `ttc-elevator-outages` | `latest`, regenerated daily | Elevator or escalator outage since collection began |

Each file set has three files: `<day>.parquet` (zstd-compressed), `<day>.csv.gz`, and
`<day>.json`. The JSON sidecar records the row count, the first and last record times,
whether the day is partial (collection began during it), and each file's size and
SHA-256. Column descriptions are in `index.json` and on the download page.

## How files are produced

`transit export` runs daily in Airflow (`open_data_export`, 09:30 UTC, after the TTC
service day ends). It writes every completed day that has not been exported yet, so a
missed run catches up the next day; `--day YYYY-MM-DD --force` rewrites a day. All files
come from one read-only database snapshot. Each file is written under a temporary name
and renamed into place, sidecar last, so a sidecar means its files are complete.
Days with no data are skipped rather than written empty.

Headway rows come from the dbt model `analytics.ttc_headways`, including rows flagged
`implausible` (two labels for one train), `spans_collection_gap` (the collector was
down) or `outside_service` (a work train before service started, so the gap spans the
overnight closure), so users can apply the same exclusions as the site or not. Bike Share rows come
from `normalized.station_observations`; apply the 30-minute freshness rule in
[metric definitions](metrics.md) before treating a count as current.

## Using the files

```python
import duckdb
duckdb.sql("""
    SELECT route_id, towards, median(headway_seconds) / 60 AS median_minutes
    FROM 'ttc-headways/2026-10-10.parquet'
    WHERE NOT implausible AND NOT spans_collection_gap AND NOT outside_service
      AND NOT is_terminal
    GROUP BY ALL ORDER BY ALL
""").show()
```

## Licence

Contains information licensed under the
[Open Government Licence – Toronto](https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/).
Sources: Toronto Transit Commission; Bike Share Toronto / Toronto Parking Authority.
The files are derived by this project and are not endorsed by either. TTC arrival times
are inferred from predictions and may be inaccurate.

## Deployment notes

Airflow writes to the `open-data` volume and the `web` service mounts it read-only at
`EXPORT_DIR`. The files can be regenerated from the database at any time, so they are
not part of the database backup. Expect about 1.5 MB per day for both daily datasets
(Parquet plus CSV).
