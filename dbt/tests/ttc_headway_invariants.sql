-- Headways are non-negative, follow the arrival order, and stay within one service day.
-- Fails the build: a violation means the model logic, not the source, is wrong.
select event_id
from {{ ref('ttc_headways') }}
where headway_seconds < 0
    or (previous_arrival_at is null) <> (headway_seconds is null)
    or (implausible and headway_seconds >= 60)
    or service_hour < 0
