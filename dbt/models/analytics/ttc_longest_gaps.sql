{{ config(materialized='table') }}
-- Ten longest measurable gaps per line and service day. A held train shows the same gap
-- at every platform downstream, so each pair of consecutive trains is one incident,
-- reported at the platform where the gap was longest.
with measured as (
    select h.*, s.scheduled_headway_seconds
    from {{ ref('ttc_headways') }} h
    left join {{ ref('ttc_scheduled_service') }} s
        using (service_date, stop_id, service_hour)
    where h.headway_seconds is not null
        and not h.is_terminal
        and not h.implausible
        and not h.spans_collection_gap
        and not h.outside_service
), incidents as (
    select
        *,
        row_number() over (
            partition by service_date, route_id, direction_id, previous_train_id, train_id
            order by headway_seconds desc, arrived_at
        ) as platform_rank
    from measured
), ranked as (
    select
        *,
        row_number() over (
            partition by service_date, route_id
            order by headway_seconds desc, arrived_at
        ) as gap_rank
    from incidents
    where platform_rank = 1
)
select
    service_date,
    route_id,
    gap_rank,
    direction_id,
    towards,
    station_key,
    station_name,
    platform,
    previous_arrival_at as gap_start,
    arrived_at as gap_end,
    headway_seconds as gap_seconds,
    scheduled_headway_seconds,
    event_id
from ranked
where gap_rank <= 10
