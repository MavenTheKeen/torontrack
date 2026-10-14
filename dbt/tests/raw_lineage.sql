select s.collection_id, s.station_id
from {{ ref('stg_station_observations') }} s
inner join {{ source('raw', 'feed_payloads') }} i on s.information_raw_id = i.raw_id
inner join {{ source('raw', 'feed_payloads') }} p on s.status_raw_id = p.raw_id
where i.collection_id <> s.collection_id or p.collection_id <> s.collection_id
    or i.feed_name <> 'station_information' or p.feed_name <> 'station_status'
