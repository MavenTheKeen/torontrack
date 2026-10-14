-- One row per route, direction and service day.
select service_date, route_id, direction_id
from {{ ref('ttc_surface_reliability_daily') }}
group by 1, 2, 3
having count(*) > 1
