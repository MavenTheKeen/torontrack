{#
    CTEs `dated` and `services` for the service dates in the CTE named `dates_cte` (one
    column, service_date): each date gets the newest TTC static GTFS version whose calendar
    covers it (else the newest loaded), and the service IDs running that day under it,
    from the weekly calendar plus added dates, less removed dates.
#}
{% macro ttc_services(dates_cte) %}
dated as (
    select d.service_date, v.feed_version
    from {{ dates_cte }} d
    cross join lateral (
        select feed_version
        from {{ source('normalized', 'ttc_gtfs_versions') }} gv
        order by (d.service_date between gv.service_start and gv.service_end) desc,
                 gv.loaded_at desc
        limit 1
    ) v
), services as (
    (
        select d.service_date, d.feed_version, c.service_id
        from dated d
        inner join {{ source('normalized', 'ttc_calendar') }} c
            on c.feed_version = d.feed_version
            and d.service_date between c.start_date and c.end_date
            and case extract(isodow from d.service_date)
                when 1 then c.monday when 2 then c.tuesday when 3 then c.wednesday
                when 4 then c.thursday when 5 then c.friday when 6 then c.saturday
                else c.sunday end
        union
        select d.service_date, d.feed_version, cd.service_id
        from dated d
        inner join {{ source('normalized', 'ttc_calendar_dates') }} cd
            on cd.feed_version = d.feed_version and cd.date = d.service_date
            and cd.exception_type = 1
    )
    except
    select d.service_date, d.feed_version, cd.service_id
    from dated d
    inner join {{ source('normalized', 'ttc_calendar_dates') }} cd
        on cd.feed_version = d.feed_version and cd.date = d.service_date
        and cd.exception_type = 2
)
{% endmacro %}
