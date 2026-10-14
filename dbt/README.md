# Availability transformations

This dbt Core project uses a separate Python 3.12 environment. The pinned
[dbt-core 1.10.13](https://pypi.org/project/dbt-core/1.10.13/) and
[dbt-postgres 1.9.1](https://pypi.org/project/dbt-postgres/1.9.1/) releases both
declare Python 3.12 support. Transitive dependencies are locked in `uv.lock`.

From the repository root, after starting PostgreSQL and collecting data:

```sh
uv sync --project dbt --locked
uv run --project dbt --locked dbt build --project-dir dbt --profiles-dir dbt
```

Connection variables are `DBT_HOST`, `DBT_PORT`, `DBT_USER`, `DBT_PASSWORD`, and
`DBT_DATABASE`. Defaults target 127.0.0.1:5432, user/database `transit`, and an
empty password; set the password to match your local database. The
[official PostgreSQL profile documentation](https://docs.getdbt.com/docs/core/connect-data-platform/postgres-setup)
describes the profile fields. Always use a separate disposable database in CI.

## Models and metric definitions

`staging.stg_station_observations` has one row per successful collection and
station metadata snapshot. A left join retains metadata snapshots that lack a
status observation. `observed` distinguishes them; missing counts stay NULL.
`collected_at` is the collector's actual status fetch time, not Airflow's logical
schedule time. Raw IDs connect each row to the two stored source payloads.

Historical freshness is measured at `status_fetched_at`. Both source publication
and station report ages must be between -300 and 1800 seconds, inclusive. The
small negative allowance tolerates source clock skew; it does not rewrite source
timestamps. `source_fresh`, `station_fresh`, and `is_fresh` remain false for missing
observations. Historical eligibility is fixed by these recorded timestamps, so
an old valid observation does not become ineligible merely because time passes.
The dashboard separately checks whether the most recent collection is current.

- Rental eligibility: observed, fresh, installed, and `is_renting` true.
- Return eligibility: observed, fresh, installed, and `is_returning` true.
- Empty snapshot: rental eligible and available bikes equal zero.
- Full snapshot: return eligible and available docks equal zero.
- Empty percentage: 100 × empty snapshots / rental-eligible snapshots.
- Full percentage: 100 × full snapshots / return-eligible snapshots.
- Observation coverage: 100 × observed snapshots / metadata snapshots.

Unknown, stale, inactive, and service-disabled observations do not enter the
corresponding empty/full denominator. A zero denominator produces NULL, not 0%.
Observation coverage measures completeness within successful collections; it
does **not** measure missed scheduled collections. The dashboard reports schedule
coverage separately. No exact empty minutes or inferred trips are produced.

`analytics.station_hourly` groups by station and **UTC hour**, with count columns
`metadata_snapshots`, `observed_snapshots`, `rental_eligible_snapshots`,
`return_eligible_snapshots`, `empty_snapshots`, and `full_snapshots`.
`sum_bikes` and `sum_docks` include only their respective eligible observations.
`mean_bikes_available`, `mean_docks_available`, `empty_pct`, `full_pct`, and
`observation_coverage_pct` derive from these counts. `first_collected_at` and
`last_collected_at` show the data window. UTC grouping preserves both hours during
Toronto's autumn daylight-saving transition.

`analytics.station_summary` adds the hourly counts before calculating the same
metrics across all collected history. It never averages hourly percentages.
Exact user-selected time periods should filter staging rows before aggregation,
because a selected interval may cut through an hour. Join the latest metadata
when a station's current name is needed; historical staging names remain intact.

## Operation and tests

All models are views. New committed data is visible immediately after the models
have been installed. This suits a small portfolio project and avoids stale
materializations or incremental-watermark machinery. Query cost will grow with
history; introduce measured indexes or incremental models if needed later.

The schema macro deliberately creates exactly `staging` and `analytics`. Unlike
dbt's usual schema-prefix convention, this project isolates environments with
separate databases. Do not point concurrent developers or CI at the same database.
The [dbt schema guidance](https://docs.getdbt.com/docs/build/custom-schemas)
explains the default convention and collision risk.

`dbt build` runs source/model not-null, uniqueness, relationships and accepted-value
tests, plus SQL checks for composite uniqueness, retained metadata coverage,
eligibility, raw lineage, and denominator invariants. Source staleness remains
visible as an excluded observation rather than an automatic schema-test failure.
Artificial metric scenarios belong only in the disposable integration-test
database. There are no production seed datasets.
