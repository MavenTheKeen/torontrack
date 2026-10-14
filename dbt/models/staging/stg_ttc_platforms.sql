-- Platforms of the most recently loaded static GTFS version, with line position.
-- Names and order rarely change; scheduled service uses the version per date instead.
with latest as (
    select feed_version
    from {{ source('normalized', 'ttc_gtfs_versions') }}
    order by loaded_at desc, feed_version
    limit 1
), line_stops as (
    select
        ls.*,
        max(ls.stop_order) over (
            partition by ls.feed_version, ls.route_id, ls.direction_id
        ) as last_stop_order
    from {{ source('normalized', 'ttc_line_stops') }} ls
    inner join latest using (feed_version)
)
select
    ls.stop_id,
    ls.route_id,
    ls.direction_id,
    ls.stop_order,
    ls.towards,
    s.station_key,
    s.station_name,
    s.platform,
    -- Trains dwell and change ends at terminals; gaps there are not headways.
    ls.stop_order in (1, ls.last_stop_order) as is_terminal
from line_stops ls
inner join {{ source('normalized', 'ttc_stops') }} s using (feed_version, stop_id)
