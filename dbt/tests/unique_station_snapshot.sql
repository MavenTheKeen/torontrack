select collection_id, station_id, count(*) as rows
from {{ ref('stg_station_observations') }}
group by collection_id, station_id
having count(*) <> 1
