{{ config(severity='warn') }}
-- Source-quality signal, not a model bug: away from terminals, fewer than 2% of a day's
-- headways should be implausibly short (two trains under a minute apart). A warning
-- surfaces a change in TTC's feed without failing the shared Bike Share build.
select service_date, route_id, count(*) filter (where implausible) as implausible, count(*) as total
from {{ ref('ttc_headways') }}
where not is_terminal and headway_seconds is not null
group by 1, 2
having count(*) >= 200 and count(*) filter (where implausible) > 0.02 * count(*)
