{{ config(materialized='table') }}
-- Road restrictions in effect along each bus and streetcar route direction on each
-- service day with measured headways. A restriction counts on a day when its dates cover
-- part of the service day (04:00 to 04:00), it was listed in the city's feed then (first
-- seen before the day ended, not removed before it began), and its weekly schedule has
-- working hours that weekday (or it runs continuously). Routes come from the match made
-- against the schedule version used for that day.
with days as (
    select distinct r.service_date, d.feed_version,
        (r.service_date::timestamp + interval '4 hours') at time zone 'America/Toronto'
            as day_start,
        (r.service_date::timestamp + interval '28 hours') at time zone 'America/Toronto'
            as day_end
    from {{ ref('ttc_surface_reliability_daily') }} r
    inner join {{ ref('ttc_service_days') }} d using (service_date)
), in_effect as (
    select d.service_date, d.feed_version, x.restriction_id, x.kind, x.max_impact
    from days d
    inner join {{ source('normalized', 'road_restrictions') }} x
        on x.starts_at < d.day_end and x.ends_at > d.day_start
        and x.first_seen_at < d.day_end
        and coalesce(x.removed_at, 'infinity') > d.day_start
        and (
            x.work_period = 'Continuous'
            or x.schedule ->> 'everyday' is not null
            or x.schedule ->> lower(trim(to_char(d.service_date, 'Day'))) is not null
        )
)
select
    e.service_date,
    m.route_id,
    m.direction_id,
    count(*) as restrictions,
    count(*) filter (where e.kind = 'closure') as closures,
    count(*) filter (where e.max_impact = 'High') as high_impact,
    sum(m.overlap_m) as overlap_m
from in_effect e
inner join {{ source('normalized', 'road_restriction_routes') }} m
    on m.restriction_id = e.restriction_id and m.feed_version = e.feed_version
group by 1, 2, 3
