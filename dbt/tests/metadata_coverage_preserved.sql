-- Both missing and extra staging rows are failures.
with expected as (
    select s.collection_id, s.station_id
    from {{ source('normalized', 'station_observations') }} s
    inner join {{ source('ops', 'ingestion_runs') }} r using (collection_id)
    where r.status = 'succeeded'
), actual as (
    select collection_id, station_id from {{ ref('stg_station_observations') }}
)
select coalesce(e.collection_id, a.collection_id) as collection_id,
    coalesce(e.station_id, a.station_id) as station_id
from expected e
full outer join actual a using (collection_id, station_id)
where e.collection_id is null or a.collection_id is null
