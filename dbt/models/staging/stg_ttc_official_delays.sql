-- TTC's own subway incident log. Where the log's line field is blank or lists several
-- lines, the line is taken from the station if only one line serves it.
with station_lines as (
    select station_key, min(route_id) as route_id, count(distinct route_id) as lines
    from {{ ref('stg_ttc_platforms') }}
    group by station_key
)
select
    d.source_id,
    d.delay_at,
    -- The TTC service day runs 04:00 to 04:00 Toronto time.
    ((d.delay_at at time zone 'America/Toronto') - interval '4 hours')::date as service_date,
    d.station_key,
    d.station_text,
    d.station_match,
    coalesce(d.route_id, case when sl.lines = 1 then sl.route_id end) as route_id,
    d.bound,
    d.code,
    c.description as code_description,
    d.min_delay,
    d.min_gap
from {{ source('normalized', 'ttc_official_delays') }} d
left join station_lines sl using (station_key)
left join {{ source('normalized', 'ttc_delay_codes') }} c using (code)
