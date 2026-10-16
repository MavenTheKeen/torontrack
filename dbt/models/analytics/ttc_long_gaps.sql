{{ config(materialized='table') }}
-- Every long gap (the live site's rule: more than twice the scheduled headway and more
-- than the scheduled headway plus 5 minutes), once per pair of consecutive trains. A held
-- train shows the same gap at every platform downstream, so each pair is reported at the
-- platform where it ran furthest over schedule, with how many platforms saw it. The web
-- app groups overlapping gaps on a line into incidents (transit.web.queries.gap_incidents).
with measured as (
    select h.*, s.scheduled_headway_seconds
    from {{ ref('ttc_headways') }} h
    inner join {{ ref('ttc_scheduled_service') }} s
        using (service_date, stop_id, service_hour)
    where h.headway_seconds is not null
        and not h.is_terminal
        and not h.implausible
        and not h.spans_collection_gap
        and not h.outside_service
        and h.headway_seconds > greatest(2 * s.scheduled_headway_seconds,
                                         s.scheduled_headway_seconds + 300)
), pairs as (
    select
        *,
        row_number() over (
            partition by service_date, route_id, direction_id, previous_train_id, train_id
            order by headway_seconds - scheduled_headway_seconds desc, arrived_at
        ) as platform_rank,
        count(*) over (
            partition by service_date, route_id, direction_id, previous_train_id, train_id
        ) as platforms
    from measured
)
select
    service_date,
    route_id,
    direction_id,
    service_hour,
    towards,
    station_key,
    station_name,
    platform,
    train_id,
    previous_arrival_at as gap_start,
    arrived_at as gap_end,
    headway_seconds as gap_seconds,
    scheduled_headway_seconds,
    round(headway_seconds - scheduled_headway_seconds) as excess_seconds,
    platforms,
    event_id
from pairs
where platform_rank = 1
