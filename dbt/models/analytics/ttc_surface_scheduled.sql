{{
    config(
        materialized='incremental',
        unique_key=['service_date', 'route_id', 'direction_id', 'stop_id', 'service_hour'],
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- Scheduled buses or streetcars per route, direction, measurement stop and service hour,
-- for each day and stop with observed passages. The schedule is summarized as trips per
-- stop pattern and hour of first departure, so a trip is placed at a stop in the hour of
-- (start hour + 30 minutes + the pattern's scheduled offset to that stop): right for most
-- trips, one hour off for some that start near the top of an hour.
with observed as (
    select distinct service_date, route_id, direction_id, stop_id
    from {{ ref('stg_ttc_surface_passages') }}
    {% if is_incremental() %}
        where service_date >= (select coalesce(max(service_date), date '1900-01-01') - 1
                               from {{ this }})
    {% endif %}
), dates as (
    select distinct service_date from observed
),
{{ ttc_services('dates') }}
select
    s.service_date,
    p.route_id,
    p.direction_id::smallint as direction_id,
    ps.stop_id,
    ((st.start_hour * 3600 + 1800 + ps.offset_seconds) / 3600)::int as service_hour,
    sum(st.trips)::int as scheduled_trips,
    round(3600.0 / sum(st.trips), 1) as scheduled_headway_seconds
from services s
inner join {{ source('normalized', 'ttc_surface_pattern_starts') }} st
    on st.feed_version = s.feed_version and st.service_id = s.service_id
inner join {{ source('normalized', 'ttc_surface_patterns') }} p
    on p.feed_version = st.feed_version and p.pattern_id = st.pattern_id
inner join {{ source('normalized', 'ttc_surface_pattern_stops') }} ps
    on ps.feed_version = st.feed_version and ps.pattern_id = st.pattern_id
inner join observed o
    on o.service_date = s.service_date and o.route_id = p.route_id
    and o.direction_id = p.direction_id and o.stop_id = ps.stop_id
group by 1, 2, 3, 4, 5
