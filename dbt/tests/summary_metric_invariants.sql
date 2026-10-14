select station_id
from {{ ref('station_summary') }}
where metadata_snapshots < observed_snapshots
    or observed_snapshots < rental_eligible_snapshots
    or observed_snapshots < return_eligible_snapshots
    or rental_eligible_snapshots < empty_snapshots
    or return_eligible_snapshots < full_snapshots
    or empty_pct not between 0 and 100
    or full_pct not between 0 and 100
    or observation_coverage_pct not between 0 and 100
    or (rental_eligible_snapshots = 0 and
        (empty_pct is not null or mean_bikes_available is not null))
    or (return_eligible_snapshots = 0 and
        (full_pct is not null or mean_docks_available is not null))
    or (rental_eligible_snapshots > 0 and empty_pct is null)
    or (return_eligible_snapshots > 0 and full_pct is null)
