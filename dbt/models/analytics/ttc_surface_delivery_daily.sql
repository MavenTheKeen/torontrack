{{
    config(
        materialized='incremental',
        unique_key='service_date',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- How much scheduled bus and streetcar service was seen on the live feed, per route,
-- direction and service day. Each measurement stop gets a day's share of its scheduled
-- vehicles that passed. Stops mostly undercount (a vehicle near a terminal or on a branch
-- can be missed), so the route's figure is the upper quartile of its stops, not the mean:
-- most fully run routes read 95 to 100%. Hours overlapping a collection gap are left out.
-- An empty hour has at least 4 vehicles scheduled at every measured stop and none seen.
-- Not proof of cancellation: a vehicle with its tracker off looks the same. Stops come
-- from ttc_surface_scheduled, so a route with no passage all day has no row. GTFS hours
-- from 28 (04:00 the next morning) belong to the next service day, which starts at 04:00.
{% if is_incremental() %}
    {% set since = "(select coalesce(max(service_date), date '1900-01-01') - 1 from " ~ this ~ ")" %}
{% endif %}
with gaps as materialized (
    select gap_start, gap_end from {{ ref('stg_ttc_vehicle_gaps') }}
), seen as (
    select
        service_date,
        service_hour,
        route_id,
        direction_id,
        stop_id,
        -- A vehicle reported twice within a minute at one stop passed it once.
        count(distinct (vehicle_id, date_trunc('minute', passed_at)))::int as observed
    from {{ ref('stg_ttc_surface_passages') }}
    {% if is_incremental() %}
        where service_date >= {{ since }}
    {% endif %}
    group by 1, 2, 3, 4, 5
), scheduled as (
    select
        case when service_hour >= 28 then service_date + 1 else service_date end as service_date,
        case when service_hour >= 28 then service_hour - 24 else service_hour end as service_hour,
        route_id,
        direction_id,
        stop_id,
        sum(scheduled_trips) as scheduled_trips
    from {{ ref('ttc_surface_scheduled') }}
    {% if is_incremental() %}
        where service_date >= {{ since }} - 1
    {% endif %}
    group by 1, 2, 3, 4, 5
), hours as (
    select
        s.service_date,
        s.service_hour,
        s.route_id,
        s.direction_id,
        s.stop_id,
        s.scheduled_trips,
        coalesce(o.observed, 0) as observed
    from scheduled s
    left join seen o using (service_date, service_hour, route_id, direction_id, stop_id)
    where not exists (
        select 1 from gaps g
        where g.gap_start < (s.service_date::timestamp at time zone 'America/Toronto')
                + make_interval(hours => s.service_hour + 1)
            and g.gap_end > (s.service_date::timestamp at time zone 'America/Toronto')
                + make_interval(hours => s.service_hour)
    )
    -- The morning after the newest passage has not happened yet.
    and s.service_date <= (select max(service_date) from seen)
    {% if is_incremental() %}
        and s.service_date >= {{ since }}
    {% endif %}
), stop_days as (
    select
        service_date,
        route_id,
        direction_id,
        stop_id,
        sum(scheduled_trips) as scheduled,
        sum(observed) as observed
    from hours
    group by 1, 2, 3, 4
    having sum(scheduled_trips) >= 15
), empty_hours as (
    select service_date, route_id, direction_id, count(*) as empty_hours
    from (
        select service_date, service_hour, route_id, direction_id
        from hours
        group by 1, 2, 3, 4
        having min(scheduled_trips) >= 4 and sum(observed) = 0
    ) h
    group by 1, 2, 3
)
select
    d.service_date,
    d.route_id,
    d.direction_id,
    count(*)::int as stops,
    (percentile_cont(0.5) within group (order by d.scheduled))::int as scheduled_trips,
    round(least(1, percentile_cont(0.75) within group (
        order by d.observed::float8 / d.scheduled
    ))::numeric, 3) as delivered,
    coalesce(max(e.empty_hours), 0)::int as empty_hours
from stop_days d
left join empty_hours e using (service_date, route_id, direction_id)
group by 1, 2, 3
