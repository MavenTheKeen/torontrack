{{
    config(
        materialized='incremental',
        unique_key=['service_date', 'stop_id', 'service_hour'],
        incremental_strategy='delete+insert',
        on_schema_change='fail',
    )
}}
-- Scheduled trains per platform and service hour on each day with observations. Each
-- date uses the newest static GTFS version whose calendar covers it, so a later schedule
-- never rewrites the comparison for earlier days. Dates come from arrivals, not
-- headways, because ttc_headways uses this model to flag gaps outside service.
with dates as (
    select distinct service_date from {{ ref('stg_ttc_arrivals') }}
    {% if is_incremental() %}
        where service_date >= (select coalesce(max(service_date), date '1900-01-01') - 1
                               from {{ this }})
    {% endif %}
),
{{ ttc_services('dates') }}
select
    s.service_date,
    st.stop_id,
    st.arrival_seconds / 3600 as service_hour,
    min(s.feed_version) as feed_version,
    count(*) as scheduled_trains,
    round(3600.0 / count(*), 1) as scheduled_headway_seconds,
    min(st.arrival_seconds) as first_arrival_seconds,
    max(st.arrival_seconds) as last_arrival_seconds
from services s
inner join {{ source('normalized', 'ttc_trips') }} t
    on t.feed_version = s.feed_version and t.service_id = s.service_id
inner join {{ source('normalized', 'ttc_stop_times') }} st
    on st.feed_version = t.feed_version and st.trip_id = t.trip_id
where st.arrival_seconds is not null
group by 1, 2, 3
