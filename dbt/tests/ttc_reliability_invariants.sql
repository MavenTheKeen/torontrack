-- Aggregate counts and percentiles must be internally consistent.
select service_date, route_id, direction_id, service_hour
from {{ ref('ttc_headway_reliability_hourly') }}
where observed_headways <= 0
    or compared_headways > observed_headways
    or regular_headways > compared_headways
    or long_gaps > compared_headways
    or regular_headways + long_gaps > compared_headways
    or median_headway_seconds > p90_headway_seconds
    or p90_headway_seconds > max_headway_seconds
    or (compared_headways = 0 and scheduled_headway_seconds is not null)
