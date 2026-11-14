{{
    config(
        materialized='incremental',
        unique_key='service_date',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- Scheduled bus and streetcar trips one by one, per route, direction and service day:
-- how many no vehicle reported running (TTC's live trip IDs are those of the timetable).
-- A trip counts as run when a vehicle reported its trip ID from an hour before its
-- scheduled start to an hour after its scheduled end; a short run is one reported for
-- under half its scheduled length (cut short, or joined late). Only trips that started
-- after collection began, ended at least an hour before the newest report, and do not
-- overlap a collection gap are counted. Not proof of cancellation: a vehicle with its
-- tracker off, or running under another trip ID, looks the same.
{% if is_incremental() %}
    {% set since = "(select coalesce(max(service_date), date '1900-01-01') - 1 from " ~ this ~ ")" %}
{% endif %}
with seen as materialized (
    select service_date, trip_id, first_seen_at, last_seen_at
    from {{ source('normalized', 'ttc_surface_trips_seen') }}
    {% if is_incremental() %}
        where service_date >= {{ since }}
    {% endif %}
), bounds as (
    select min(first_seen_at) as began, max(last_seen_at) as newest
    from {{ source('normalized', 'ttc_surface_trips_seen') }}
), gaps as materialized (
    select gap_start, gap_end from {{ ref('stg_ttc_vehicle_gaps') }}
), dates as (
    select distinct service_date from seen
),
{{ ttc_services('dates') }},
scheduled as (
    select
        s.service_date,
        t.trip_id,
        p.route_id,
        p.direction_id::smallint as direction_id,
        t.end_seconds - t.start_seconds as scheduled_seconds,
        (s.service_date::timestamp + make_interval(secs => t.start_seconds))
            at time zone 'America/Toronto' as starts_at,
        (s.service_date::timestamp + make_interval(secs => t.end_seconds))
            at time zone 'America/Toronto' as ends_at
    from services s
    inner join {{ source('normalized', 'ttc_surface_trips') }} t
        on t.feed_version = s.feed_version and t.service_id = s.service_id
    inner join {{ source('normalized', 'ttc_surface_patterns') }} p
        on p.feed_version = t.feed_version and p.pattern_id = t.pattern_id
), measurable as (
    select sc.*
    from scheduled sc
    cross join bounds b
    where sc.starts_at >= b.began
        and sc.ends_at + interval '1 hour' <= b.newest
        and not exists (
            select 1 from gaps g
            where g.gap_start < sc.ends_at and g.gap_end > sc.starts_at
        )
), matched as (
    -- One join on trip ID instead of a lookup per scheduled trip: the materialized `seen`
    -- has no index, so a per-trip lookup scanned it once for every trip.
    -- A trip after midnight can be recorded under the next service day.
    select
        m.service_date,
        m.route_id,
        m.direction_id,
        m.scheduled_seconds,
        -- least() and greatest() skip nulls, so an unmatched trip must stay null here.
        max(extract(epoch from least(x.last_seen_at, m.ends_at)
            - greatest(x.first_seen_at, m.starts_at)))
            filter (where x.trip_id is not null) as reported_seconds
    from measurable m
    left join seen x
        on x.trip_id = m.trip_id
        and x.service_date in (m.service_date, m.service_date + 1)
        and x.first_seen_at < m.ends_at + interval '1 hour'
        and x.last_seen_at > m.starts_at - interval '1 hour'
    group by m.service_date, m.trip_id, m.route_id, m.direction_id, m.scheduled_seconds,
        m.starts_at, m.ends_at
)
select
    service_date,
    route_id,
    direction_id,
    count(*)::int as scheduled_trips,
    count(*) filter (where reported_seconds is null)::int as unseen_trips,
    count(*) filter (
        where reported_seconds is not null and reported_seconds < 0.5 * scheduled_seconds
    )::int as short_trips
from matched
group by 1, 2, 3
