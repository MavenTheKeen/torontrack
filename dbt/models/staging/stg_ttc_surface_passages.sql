-- One row per bus or streetcar passing a measurement stop, with the TTC service day
-- (04:00 to 04:00 Toronto time) and the hour counted from its local midnight, which can
-- pass 23 after midnight like GTFS times.
with dated as (
    select
        *,
        ((passed_at at time zone 'America/Toronto') - interval '4 hours')::date
            as service_date
    from {{ source('normalized', 'ttc_surface_passages') }}
)
select
    passed_at,
    service_date,
    floor(
        extract(epoch from passed_at
            - (service_date::timestamp at time zone 'America/Toronto')) / 3600
    )::int as service_hour,
    route_id,
    direction_id,
    stop_id,
    vehicle_id,
    pattern_id,
    report_gap_seconds
from dated
