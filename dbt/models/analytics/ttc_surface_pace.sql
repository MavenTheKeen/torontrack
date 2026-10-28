{{ config(materialized='table') }}
-- How many seconds of timetable a route's vehicles cover per real second, by route,
-- direction and hour of day (Toronto time), from consecutive stop passages of the same
-- vehicle on the same pattern over the last 28 days. The map moves buses at this pace
-- between position reports. Legs under a minute of timetable, and those taking over four
-- times it (a layover or a detour), are left out. Built here rather than per request:
-- over 28 days of passages it takes several seconds.
with version as (
    select feed_version
    from {{ source('normalized', 'ttc_gtfs_versions') }}
    order by loaded_at desc, feed_version
    limit 1
), passages as (
    select
        route_id,
        direction_id,
        pattern_id,
        stop_id,
        passed_at,
        lag(stop_id) over w as previous_stop_id,
        lag(passed_at) over w as previous_passed_at,
        lag(pattern_id) over w as previous_pattern_id
    from {{ source('normalized', 'ttc_surface_passages') }}
    where passed_at > now() - interval '28 days'
    window w as (partition by vehicle_id order by passed_at)
), legs as (
    select
        p.route_id,
        p.direction_id,
        extract(hour from p.passed_at at time zone 'America/Toronto')::int as hour,
        extract(epoch from p.passed_at - p.previous_passed_at) as actual_seconds,
        b.offset_seconds - a.offset_seconds as scheduled_seconds
    from passages p
    cross join version v
    join {{ source('normalized', 'ttc_surface_pattern_stops') }} a
        on a.feed_version = v.feed_version
        and a.pattern_id = p.pattern_id
        and a.stop_id = p.previous_stop_id
    join {{ source('normalized', 'ttc_surface_pattern_stops') }} b
        on b.feed_version = v.feed_version
        and b.pattern_id = p.pattern_id
        and b.stop_id = p.stop_id
    where p.previous_pattern_id = p.pattern_id
)
select
    (select feed_version from version) as feed_version,
    route_id,
    direction_id,
    hour,
    sum(scheduled_seconds) / sum(actual_seconds) as pace
from legs
where scheduled_seconds > 60
    and actual_seconds > 0
    and actual_seconds < scheduled_seconds * 4
group by route_id, direction_id, hour
having sum(actual_seconds) > 600
