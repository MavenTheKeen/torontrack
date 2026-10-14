-- Hourly weather at Toronto City (downtown), keyed like the TTC models: the service
-- day runs 04:00 to 04:00 Toronto time and service_hour counts from its local midnight,
-- so 24 to 27 are after midnight.
--
-- ECCC does not document whether an hour's precipitation covers the hour before or after
-- its timestamp, so the hour starting at observed_at uses the observations at both ends:
-- wet if either recorded precipitation, and their mean temperature. That is right under
-- either convention.
with hours as (
    select
        s.observed_at,
        greatest(s.precip_mm, e.precip_mm) as precip_mm,
        round((s.temp_c + coalesce(e.temp_c, s.temp_c)) / 2, 1) as temp_c,
        s.relative_humidity,
        s.wind_speed_kmh,
        s.flags || coalesce(e.flags, '{}') as flags
    from {{ source('normalized', 'weather_hourly') }} s
    left join {{ source('normalized', 'weather_hourly') }} e
        on e.climate_id = s.climate_id and e.observed_at = s.observed_at + interval '1 hour'
    where s.climate_id = '6158355'
)
select
    observed_at,
    ((observed_at at time zone 'America/Toronto') - interval '4 hours')::date as service_date,
    floor(
        extract(epoch from observed_at
            - (((observed_at at time zone 'America/Toronto') - interval '4 hours')::date::timestamp
               at time zone 'America/Toronto')) / 3600
    )::int as service_hour,
    temp_c,
    relative_humidity,
    precip_mm,
    case
        when precip_mm is null then null
        when precip_mm = 0 then 'dry'
        when temp_c <= 0 then 'snow'
        else 'rain'
    end as conditions,
    wind_speed_kmh,
    flags
from hours
