-- Periods with no new trip-update snapshot for over 2 minutes (collector or feed down).
-- A headway spanning one of these cannot be measured and is excluded from metrics.
with snapshots as (
    select distinct feed_timestamp
    from {{ source('ops', 'ttc_poll_runs') }}
    where feed = 'trips_subway' and status = 'stored'
), ordered as (
    select
        feed_timestamp as gap_start,
        lead(feed_timestamp) over (order by feed_timestamp) as gap_end
    from snapshots
)
select
    gap_start,
    gap_end,
    extract(epoch from gap_end - gap_start)::int as gap_seconds
from ordered
where gap_end - gap_start > interval '120 seconds'
