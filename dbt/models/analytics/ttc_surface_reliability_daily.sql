{{ config(materialized='table') }}
-- Bus and streetcar reliability per route, direction and service day, from headways at
-- measurement stops. Only measurable headways count (not the same vehicle twice, not
-- spanning a collection gap), and only where the schedule has a vehicle at least every
-- 15 minutes: on less frequent routes riders time their trips to the schedule, so the
-- gap between buses is not what they wait.
--
-- Bunched: a headway under a quarter of the scheduled one (vehicles arriving together).
-- Long gap: the subway rule, more than twice the scheduled headway and more than it plus
-- 5 minutes. Excess wait time uses the same sums as the subway report.
with measured as (
    select *
    from {{ ref('ttc_surface_headways') }}
    where not same_vehicle
        and not spans_collection_gap
        and scheduled_headway_seconds <= 900
)
select
    m.service_date,
    d.day_type,
    m.route_id,
    m.direction_id,
    count(*) as headways,
    count(distinct m.stop_id) as stops,
    percentile_cont(0.5) within group (order by headway_seconds) as median_headway_seconds,
    round(avg(scheduled_headway_seconds), 1) as scheduled_headway_seconds,
    count(*) filter (where headway_seconds < 0.25 * scheduled_headway_seconds) as bunched,
    count(*) filter (
        where headway_seconds > greatest(2 * scheduled_headway_seconds,
                                         scheduled_headway_seconds + 300)
    ) as long_gaps,
    sum(headway_seconds) as compared_seconds,
    sum(headway_seconds::numeric ^ 2) as compared_squared_seconds,
    sum(headway_seconds * scheduled_headway_seconds) as compared_scheduled_product
from measured m
inner join {{ ref('ttc_service_days') }} d using (service_date)
group by 1, 2, 3, 4
