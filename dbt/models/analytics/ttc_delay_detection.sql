{{ config(materialized='table') }}
-- Did this project observe the gap behind each incident TTC logged? For every logged
-- incident with a gap between trains, at a known station and line, during a period this
-- project was collecting: the longest gap observed at that station and line (and
-- direction, when the log gives one) overlapping the incident. An incident counts as
-- detected when that observed gap is at least 75% of the gap TTC reported; observed
-- gaps are measured between inferred arrivals, so they rarely match to the minute.
with coverage as (
    select min(previous_arrival_at) as starts, max(arrived_at) as ends
    from {{ ref('ttc_headways') }}
), incidents as (
    select d.*
    from {{ ref('stg_ttc_official_delays') }} d
    cross join coverage
    where d.min_gap > 0
      and d.station_key is not null
      and d.route_id is not null
      and d.delay_at >= coverage.starts
      and d.delay_at <= coverage.ends
), windows as (
    select
        *,
        delay_at - interval '5 minutes' as window_start,
        delay_at + make_interval(mins => min_gap + 5) as window_end
    from incidents
), observed as (
    select
        w.source_id,
        count(h.event_id) as observed_headways,
        max(h.headway_seconds) as observed_max_gap_seconds
    from windows w
    left join {{ ref('ttc_headways') }} h
        on h.station_key = w.station_key
       and h.route_id = w.route_id
       and (w.bound is null or split_part(h.platform, ' ', 1) = w.bound)
       and not h.implausible
       and not h.outside_service
       and h.previous_arrival_at < w.window_end
       and h.arrived_at > w.window_start
    group by w.source_id
)
select
    w.source_id,
    w.delay_at,
    w.service_date,
    w.route_id,
    w.station_key,
    w.station_match,
    w.bound,
    w.code,
    w.code_description,
    w.min_delay,
    w.min_gap,
    -- An incident while the collector was down cannot be checked either way.
    not exists (
        select 1 from {{ ref('stg_ttc_collection_gaps') }} g
        where g.gap_start < w.window_end and g.gap_end > w.window_start
    ) as in_coverage,
    o.observed_headways,
    o.observed_max_gap_seconds,
    coalesce(o.observed_max_gap_seconds >= 0.75 * w.min_gap * 60, false) as detected
from windows w
inner join observed o using (source_id)
