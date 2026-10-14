{{
    config(
        materialized='incremental',
        unique_key='event_id',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- Observed headway: time since the previous train at the same platform on the same
-- service day. Incremental, so history outlives the 90-day train-visit retention; each
-- run rebuilds the last two service days (late "passed" marks) with one day of context.
{% if is_incremental() %}
    {% set since = "(select coalesce(max(service_date), date '1900-01-01') - 1 from " ~ this ~ ")" %}
{% endif %}
with arrivals as (
    select *
    from {{ ref('stg_ttc_arrivals') }}
    {% if is_incremental() %}
        where service_date >= {{ since }} - 1
    {% endif %}
), ordered as (
    select
        *,
        lag(arrived_at) over w as previous_arrival_at,
        lag(train_id) over w as previous_train_id
    from arrivals
    window w as (partition by stop_id, service_date order by arrived_at, event_id)
)
select
    o.event_id,
    o.service_date,
    o.service_hour,
    o.route_id,
    o.direction_id,
    o.stop_id,
    o.station_key,
    o.station_name,
    o.platform,
    o.towards,
    o.is_terminal,
    o.train_id,
    o.previous_train_id,
    o.previous_arrival_at,
    o.arrived_at,
    extract(epoch from o.arrived_at - o.previous_arrival_at)::int as headway_seconds,
    -- Two trains cannot serve one platform under a minute apart; seen at terminals where
    -- trains change ends (and labels).
    o.previous_arrival_at is not null
        and o.arrived_at - o.previous_arrival_at < interval '60 seconds' as implausible,
    exists (
        select 1 from {{ ref('stg_ttc_collection_gaps') }} g
        where g.gap_start < o.arrived_at and g.gap_end > o.previous_arrival_at
    ) as spans_collection_gap,
    -- A train passed more than 10 minutes before the platform's first scheduled train
    -- or 30 minutes after its last: a work or positioning train, so the gap covers the
    -- overnight closure rather than a delay.
    o.previous_arrival_at is not null and coalesce(
        o.previous_arrival_at < span.service_start - interval '10 minutes'
            or o.arrived_at > span.service_end + interval '30 minutes',
        true
    ) as outside_service
from ordered o
left join lateral (
    select
        day_start + min(s.first_arrival_seconds) * interval '1 second' as service_start,
        day_start + max(s.last_arrival_seconds) * interval '1 second' as service_end
    from {{ ref('ttc_scheduled_service') }} s
    cross join lateral (
        select o.service_date::timestamp at time zone 'America/Toronto' as day_start
    ) d
    where s.service_date = o.service_date and s.stop_id = o.stop_id
    group by day_start
) span on true
{% if is_incremental() %}
    where o.service_date >= {{ since }}
{% endif %}
