-- One usual pace per route, direction and hour.
select route_id, direction_id, hour
from {{ ref('ttc_surface_pace') }}
group by route_id, direction_id, hour
having count(*) > 1
