-- Add counts before dividing: averaging hourly percentages biases sparse hours.
with counts as (
    select
        station_id,
        min(first_collected_at) as first_collected_at,
        max(last_collected_at) as last_collected_at,
        sum(metadata_snapshots) as metadata_snapshots,
        sum(observed_snapshots) as observed_snapshots,
        sum(rental_eligible_snapshots) as rental_eligible_snapshots,
        sum(return_eligible_snapshots) as return_eligible_snapshots,
        sum(empty_snapshots) as empty_snapshots,
        sum(full_snapshots) as full_snapshots,
        sum(sum_bikes) as sum_bikes,
        sum(sum_docks) as sum_docks
    from {{ ref('station_hourly') }}
    group by station_id
)
select
    *,
    sum_bikes::numeric / nullif(rental_eligible_snapshots, 0) as mean_bikes_available,
    sum_docks::numeric / nullif(return_eligible_snapshots, 0) as mean_docks_available,
    100.0 * empty_snapshots / nullif(rental_eligible_snapshots, 0) as empty_pct,
    100.0 * full_snapshots / nullif(return_eligible_snapshots, 0) as full_pct,
    100.0 * observed_snapshots / nullif(metadata_snapshots, 0) as observation_coverage_pct
from counts
