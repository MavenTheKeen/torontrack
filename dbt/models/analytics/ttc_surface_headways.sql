{{
    config(
        materialized='incremental',
        unique_key='service_date',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- Observed bus and streetcar headways: time since the previous vehicle of the same route
-- and direction passed the same measurement stop on the same service day, with the
-- scheduled headway for that hour. Incremental, so history outlives the 90-day passage
-- retention; each run rebuilds the last two service days with one day of context.
{% if is_incremental() %}
    {% set since = "(select coalesce(max(service_date), date '1900-01-01') - 1 from " ~ this ~ ")" %}
{% endif %}
with passages as (
    select * from {{ ref('stg_ttc_surface_passages') }}
    {% if is_incremental() %}
        where service_date >= {{ since }} - 1
    {% endif %}
), ordered as (
    select
        *,
        lag(passed_at) over w as previous_passed_at,
        lag(vehicle_id) over w as previous_vehicle_id
    from passages
    window w as (
        partition by route_id, direction_id, stop_id, service_date
        order by passed_at, vehicle_id
    )
)
select
    o.service_date,
    o.service_hour,
    o.route_id,
    o.direction_id,
    o.stop_id,
    o.vehicle_id,
    o.previous_vehicle_id,
    o.previous_passed_at,
    o.passed_at,
    extract(epoch from o.passed_at - o.previous_passed_at)::int as headway_seconds,
    s.scheduled_headway_seconds,
    -- The same vehicle twice in a row looped back past the stop: not a headway.
    o.vehicle_id = o.previous_vehicle_id as same_vehicle,
    exists (
        select 1 from {{ ref('stg_ttc_vehicle_gaps') }} g
        where g.gap_start < o.passed_at and g.gap_end > o.previous_passed_at
    ) as spans_collection_gap
from ordered o
left join {{ ref('ttc_surface_scheduled') }} s
    on s.service_date = o.service_date and s.route_id = o.route_id
    and s.direction_id = o.direction_id and s.stop_id = o.stop_id
    and s.service_hour = o.service_hour
where o.previous_passed_at is not null
{% if is_incremental() %}
    and o.service_date >= {{ since }}
{% endif %}
