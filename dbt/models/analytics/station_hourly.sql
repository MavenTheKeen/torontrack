with counts as (
    select
        station_id,
        date_trunc('hour', collected_at, 'UTC') as hour_utc,
        min(collected_at) as first_collected_at,
        max(collected_at) as last_collected_at,
        count(*) as metadata_snapshots,
        count(*) filter (where observed) as observed_snapshots,
        count(*) filter (where rental_eligible) as rental_eligible_snapshots,
        count(*) filter (where return_eligible) as return_eligible_snapshots,
        count(*) filter (where is_empty) as empty_snapshots,
        count(*) filter (where is_full) as full_snapshots,
        coalesce(sum(num_bikes_available) filter (where rental_eligible), 0) as sum_bikes,
        coalesce(sum(num_docks_available) filter (where return_eligible), 0) as sum_docks
    from {{ ref('stg_station_observations') }}
    group by station_id, date_trunc('hour', collected_at, 'UTC')
)
select
    *,
    sum_bikes::numeric / nullif(rental_eligible_snapshots, 0) as mean_bikes_available,
    sum_docks::numeric / nullif(return_eligible_snapshots, 0) as mean_docks_available,
    100.0 * empty_snapshots / nullif(rental_eligible_snapshots, 0) as empty_pct,
    100.0 * full_snapshots / nullif(return_eligible_snapshots, 0) as full_pct,
    100.0 * observed_snapshots / nullif(metadata_snapshots, 0) as observation_coverage_pct
from counts
