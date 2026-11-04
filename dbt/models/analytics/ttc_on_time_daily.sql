{{
    config(
        materialized='incremental',
        unique_key='service_date',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- How long a rider must allow on frequent TTC service, per route, direction, part of the
-- day and completed service day. Wait: the 90th percentile for a rider arriving at a random
-- moment, taking one point per minute of each measured headway, so long gaps count by their
-- length. Ride (surface only): the 90th percentile of actual over scheduled time for one
-- vehicle between stops three measurement stops apart, about 30 scheduled minutes.
-- A build handles only the next unprocessed day: a day takes seconds, every day at once
-- would take minutes, and rides are too costly to recompute every 15 minutes.
with day as (
    select
        service_date,
        case when day_type = 'weekday' then 'weekday' else 'weekend' end as day_kind
    from {{ ref('ttc_service_days') }}
    where service_date < ((now() at time zone 'America/Toronto') - interval '4 hours')::date
    {% if is_incremental() %}
        and service_date > (select coalesce(max(service_date), date '1900-01-01') from {{ this }})
    {% endif %}
    order by service_date
    limit 1
), versions as (
    -- A passage's pattern belongs to the schedule loaded when it was recorded.
    select
        feed_version,
        case when row_number() over (order by loaded_at) = 1 then '-infinity'::timestamptz
             else loaded_at end as valid_from,
        coalesce(lead(loaded_at) over (order by loaded_at), 'infinity'::timestamptz) as valid_to
    from {{ source('normalized', 'ttc_gtfs_versions') }}
), passages as (
    select
        p.*,
        lag(passed_at, 3) over w as previous_at,
        lag(stop_id, 3) over w as previous_stop_id,
        lag(pattern_id, 3) over w as previous_pattern_id
    from {{ source('normalized', 'ttc_surface_passages') }} p
    where p.passed_at >= (select (service_date::timestamp + interval '4 hours')
                                 at time zone 'America/Toronto' from day)
        and p.passed_at < (select (service_date::timestamp + interval '28 hours')
                                  at time zone 'America/Toronto' from day)
    window w as (partition by vehicle_id order by passed_at)
), rides as (
    select
        p.route_id,
        p.direction_id,
        {{ time_band("extract(hour from p.previous_at at time zone 'America/Toronto')") }} as band,
        extract(epoch from p.passed_at - p.previous_at)::float8
            / (b.offset_seconds - a.offset_seconds) as ratio
    from passages p
    inner join versions v on p.previous_at >= v.valid_from and p.previous_at < v.valid_to
    inner join {{ source('normalized', 'ttc_surface_pattern_stops') }} a
        on a.feed_version = v.feed_version and a.pattern_id = p.pattern_id
        and a.stop_id = p.previous_stop_id
    inner join {{ source('normalized', 'ttc_surface_pattern_stops') }} b
        on b.feed_version = v.feed_version and b.pattern_id = p.pattern_id
        and b.stop_id = p.stop_id
    where p.previous_pattern_id = p.pattern_id
        and b.offset_seconds > a.offset_seconds
), ride_days as (
    select
        route_id,
        direction_id,
        band,
        percentile_cont(0.9) within group (order by ratio) as ride_ratio_p90,
        count(*)::int as rides
    from rides
    where ratio < 5
    group by 1, 2, 3
), gaps as (
    select route_id, direction_id, {{ time_band('service_hour') }} as band, headway_seconds
    from {{ ref('ttc_surface_headways') }}
    where service_date = (select service_date from day)
        and not same_vehicle
        and not spans_collection_gap
        and scheduled_headway_seconds <= 900
        and headway_seconds < 7200
    union all
    select route_id, direction_id, {{ time_band('service_hour') }} as band, headway_seconds
    from {{ ref('ttc_headways') }}
    where service_date = (select service_date from day)
        and not implausible
        and not spans_collection_gap
        and not outside_service
        and not is_terminal
        and headway_seconds < 7200
), wait_days as (
    select
        g.route_id,
        g.direction_id,
        g.band,
        percentile_cont(0.9) within group (order by (m.minute - 0.5) * 60) as wait_p90_seconds,
        count(*)::int as wait_minutes
    from gaps g
    cross join lateral generate_series(1, greatest(1, g.headway_seconds / 60)) as m (minute)
    group by 1, 2, 3
)
select
    d.service_date,
    d.day_kind,
    w.route_id,
    w.direction_id,
    w.band,
    round(w.wait_p90_seconds::numeric, 1) as wait_p90_seconds,
    w.wait_minutes,
    round(r.ride_ratio_p90::numeric, 3) as ride_ratio_p90,
    r.rides
from wait_days w
cross join day d
left join ride_days r using (route_id, direction_id, band)
