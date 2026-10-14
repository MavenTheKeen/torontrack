{{ config(materialized='table') }}
-- Observed versus scheduled headways per line, direction and service hour, with the
-- day type and the weather that hour. Only measurable headways count: not at terminals,
-- not implausibly short, not spanning a collection gap, and not outside scheduled
-- service. A long gap uses the same rule as the live site: more than twice the scheduled
-- headway and more than the headway plus 5 minutes.
--
-- Excess wait time (EWT) needs three sums over the headways compared with the schedule:
-- a rider arriving at a random moment waits sum(h^2) / (2 sum(h)) on average, and the
-- schedule promises sum(h * s) / (2 sum(h)), where s is the scheduled headway. EWT is
-- the difference. Sums, not averages, so hours, days and lines can be combined.
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
)
select
    m.service_date,
    d.day_type,
    m.route_id,
    m.direction_id,
    m.service_hour,
    w.conditions,
    w.temp_c,
    count(*) as observed_headways,
    count(scheduled_headway_seconds) as compared_headways,
    percentile_cont(0.5) within group (order by headway_seconds) as median_headway_seconds,
    percentile_cont(0.9) within group (order by headway_seconds) as p90_headway_seconds,
    max(headway_seconds) as max_headway_seconds,
    round(avg(scheduled_headway_seconds), 1) as scheduled_headway_seconds,
    count(*) filter (
        where headway_seconds <= 1.5 * scheduled_headway_seconds
    ) as regular_headways,
    count(*) filter (
        where headway_seconds > greatest(2 * scheduled_headway_seconds,
                                         scheduled_headway_seconds + 300)
    ) as long_gaps,
    sum(headway_seconds) filter (where scheduled_headway_seconds is not null)
        as compared_seconds,
    sum(headway_seconds::numeric ^ 2) filter (where scheduled_headway_seconds is not null)
        as compared_squared_seconds,
    sum(headway_seconds * scheduled_headway_seconds) as compared_scheduled_product
from measured m
inner join {{ ref('ttc_service_days') }} d using (service_date)
left join {{ ref('stg_weather_hourly') }} w using (service_date, service_hour)
group by 1, 2, 3, 4, 5, 6, 7
