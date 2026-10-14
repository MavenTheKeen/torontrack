-- Elevator and escalator outages from TTC accessibility alerts. Duration runs from when
-- this project first saw the alert, so outages already in progress when collection began
-- are marked as lower bounds.
with collection as (
    select min(first_seen_at) as started_at
    from {{ source('normalized', 'ttc_alerts') }}
    where feed = 'alerts_accessibility'
), alerts as (
    select
        a.alert_id,
        a.first_seen_at,
        a.last_seen_at,
        a.last_snapshot_id = s.snapshot_id as active,
        v.header_text,
        v.stop_ids
    from {{ source('normalized', 'ttc_alerts') }} a
    inner join {{ source('normalized', 'ttc_alert_versions') }} v
        on v.feed = a.feed and v.alert_id = a.alert_id
        and v.version_hash = a.current_version_hash
    left join {{ source('ops', 'ttc_feed_state') }} s on s.feed = a.feed
    where a.feed = 'alerts_accessibility'
)
select
    a.alert_id,
    coalesce(p.station_key, 'unknown') as station_key,
    coalesce(p.station_name, split_part(a.header_text, ':', 1)) as station_name,
    case
        when a.header_text ~* 'elevator' then 'elevator'
        when a.header_text ~* 'escalator' then 'escalator'
        else 'other'
    end as device_type,
    a.header_text,
    a.first_seen_at,
    case when a.active then null else a.last_seen_at end as resolved_at,
    a.active,
    round(extract(epoch from
        coalesce(case when a.active then null else a.last_seen_at end, now())
        - a.first_seen_at) / 60)::int as duration_minutes,
    a.first_seen_at <= c.started_at + interval '5 minutes' as began_before_collection
from alerts a
cross join collection c
left join lateral (
    select station_key, station_name
    from {{ ref('stg_ttc_platforms') }}
    where stop_id = any(a.stop_ids)
    order by station_key
    limit 1
) p on true
