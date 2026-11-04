{{ config(materialized='table') }}
-- The last 28 measured days of ttc_on_time_daily per route, direction, part of the day and
-- weekday or weekend, for the trip planner (docs/planner.md#on-time-estimates).
select
    day_kind,
    route_id,
    direction_id,
    band,
    round(avg(wait_p90_seconds))::int as wait_p90_seconds,
    round(avg(ride_ratio_p90), 3) as ride_ratio_p90,
    count(*)::int as days
from {{ ref('ttc_on_time_daily') }}
where service_date > (select max(service_date) - 28 from {{ ref('ttc_on_time_daily') }})
group by 1, 2, 3, 4
