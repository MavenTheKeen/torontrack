{{
    config(
        materialized='incremental',
        unique_key='service_date',
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- Day type for each observed service day, from TTC's own calendar rather than a list of
-- holidays: a date whose regular service is removed by a calendar exception runs a
-- holiday schedule (Thanksgiving, 2026-10-12, removed weekday service and added another).
-- Incremental, so days outlive the 90-day train-visit retention that its dates come from.
with dates as (
    select distinct service_date from {{ ref('stg_ttc_arrivals') }}
    {% if is_incremental() %}
        where service_date >= (select coalesce(max(service_date), date '1900-01-01') - 1
                               from {{ this }})
    {% endif %}
), dated as (
    select d.service_date, v.feed_version
    from dates d
    cross join lateral (
        select feed_version
        from {{ source('normalized', 'ttc_gtfs_versions') }} gv
        order by (d.service_date between gv.service_start and gv.service_end) desc,
                 gv.loaded_at desc
        limit 1
    ) v
), regular as (
    select d.service_date, d.feed_version, c.service_id
    from dated d
    inner join {{ source('normalized', 'ttc_calendar') }} c
        on c.feed_version = d.feed_version
        and d.service_date between c.start_date and c.end_date
        and case extract(isodow from d.service_date)
            when 1 then c.monday when 2 then c.tuesday when 3 then c.wednesday
            when 4 then c.thursday when 5 then c.friday when 6 then c.saturday
            else c.sunday end
)
select
    d.service_date,
    d.feed_version,
    case
        when exists (
            select 1 from regular r
            inner join {{ source('normalized', 'ttc_calendar_dates') }} cd
                on cd.feed_version = r.feed_version and cd.date = r.service_date
                and cd.service_id = r.service_id and cd.exception_type = 2
            where r.service_date = d.service_date
        ) then 'holiday'
        when extract(isodow from d.service_date) = 6 then 'saturday'
        when extract(isodow from d.service_date) = 7 then 'sunday'
        else 'weekday'
    end as day_type
from dated d
