-- One row per observed train arrival: a visit the collector saw the train pass.
-- predicted_arrival from the last snapshot that still listed the platform is the
-- best available arrival time (the subway feed has no vehicle positions).
select
    e.event_id,
    e.service_date,
    e.train_id,
    e.route_id,
    e.direction_id,
    e.stop_id,
    p.station_key,
    p.station_name,
    p.platform,
    p.towards,
    p.is_terminal,
    e.predicted_arrival as arrived_at,
    -- Hours since local midnight of the service day; can exceed 23 after midnight,
    -- matching GTFS stop_times.
    floor(
        extract(epoch from e.predicted_arrival
            - (e.service_date::timestamp at time zone 'America/Toronto')) / 3600
    )::int as service_hour,
    e.passed_at
from {{ source('normalized', 'ttc_train_stop_events') }} e
inner join {{ ref('stg_ttc_platforms') }} p using (stop_id, route_id, direction_id)
where e.passed_at is not null
