select station_id, hour_utc, count(*) as rows
from {{ ref('station_hourly') }}
group by station_id, hour_utc
having count(*) <> 1
