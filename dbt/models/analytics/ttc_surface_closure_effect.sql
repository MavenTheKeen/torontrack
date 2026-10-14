{{ config(materialized='table') }}
-- Bus and streetcar reliability on weekdays with and without a high-impact road
-- restriction along the route, per route and direction. Weekdays only, so a weekend
-- schedule is never compared with a weekday one. The report pools routes that have
-- enough days on both sides; a restriction in place for the whole collection period
-- gives its route no days without it, so that route cannot be compared.
with days as (
    select
        r.*,
        coalesce(c.high_impact, 0) > 0 as affected,
        coalesce(c.restrictions, 0) as restrictions
    from {{ ref('ttc_surface_reliability_daily') }} r
    left join {{ ref('ttc_surface_closure_days') }} c
        using (service_date, route_id, direction_id)
    where r.day_type = 'weekday'
)
select
    route_id,
    direction_id,
    affected,
    count(*) as days,
    sum(headways)::int as headways,
    sum(bunched)::int as bunched,
    sum(long_gaps)::int as long_gaps,
    sum(compared_seconds) as compared_seconds,
    sum(compared_squared_seconds) as compared_squared_seconds,
    sum(compared_scheduled_product) as compared_scheduled_product
from days
group by 1, 2, 3
