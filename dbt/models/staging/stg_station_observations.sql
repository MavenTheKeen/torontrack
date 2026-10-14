-- A station listed in metadata without a status report is still a metadata snapshot
-- and contributes to coverage; station_observations includes it with observed = false.
with snapshots as (
    select
        s.collection_id,
        s.station_id,
        s.name,
        s.lat,
        s.lon,
        s.capacity,
        r.collected_at,
        s.information_raw_id,
        s.status_raw_id,
        s.status_fetched_at,
        s.source_published_at,
        s.station_reported_at,
        s.num_bikes_available,
        s.num_docks_available,
        s.num_bikes_disabled,
        s.num_docks_disabled,
        s.is_installed,
        s.is_renting,
        s.is_returning,
        s.observed
    from {{ source('normalized', 'station_observations') }} s
    inner join {{ source('ops', 'ingestion_runs') }} r
        on s.collection_id = r.collection_id and r.status = 'succeeded'
), freshness as (
    select
        *,
        coalesce(
            observed
            and extract(epoch from status_fetched_at - source_published_at)
                between -{{ var('future_tolerance_seconds') }}
                    and {{ var('max_report_age_seconds') }},
            false
        ) as source_fresh,
        coalesce(
            observed
            and extract(epoch from status_fetched_at - station_reported_at)
                between -{{ var('future_tolerance_seconds') }}
                    and {{ var('max_report_age_seconds') }},
            false
        ) as station_fresh
    from snapshots
), eligibility as (
    select
        *,
        source_fresh and station_fresh as is_fresh,
        coalesce(observed and source_fresh and station_fresh and is_installed
            and is_renting, false) as rental_eligible,
        coalesce(observed and source_fresh and station_fresh and is_installed
            and is_returning, false) as return_eligible
    from freshness
)
select
    *,
    rental_eligible and num_bikes_available = 0 as is_empty,
    return_eligible and num_docks_available = 0 as is_full
from eligibility
