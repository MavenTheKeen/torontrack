{{ config(materialized='table') }}
-- Measurable headways per platform and service day, with the sums excess wait time
-- needs (see ttc_headway_reliability_hourly), so any range of days can be combined.
with measured as (
    select h.*, s.scheduled_headway_seconds
    from {{ ref('ttc_headways') }} h
    inner join {{ ref('ttc_scheduled_service') }} s
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
    m.stop_id,
    m.station_key,
    m.station_name,
    m.platform,
    m.towards,
    count(*) as compared_headways,
    count(*) filter (
        where m.headway_seconds <= 1.5 * m.scheduled_headway_seconds
    ) as regular_headways,
    count(*) filter (
        where m.headway_seconds > greatest(2 * m.scheduled_headway_seconds,
                                           m.scheduled_headway_seconds + 300)
    ) as long_gaps,
    sum(m.headway_seconds) as compared_seconds,
    sum(m.headway_seconds::numeric ^ 2) as compared_squared_seconds,
    sum(m.headway_seconds * m.scheduled_headway_seconds) as compared_scheduled_product
from measured m
inner join {{ ref('ttc_service_days') }} d using (service_date)
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
