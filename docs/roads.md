# Road restrictions

The map shows the City of Toronto's construction zones and road closures, and the
collector keeps their history so bus and streetcar reliability can be compared with and
without nearby closures.

Road restrictions: City of Toronto. Contains information licensed under the
[Open Government Licence – Toronto](https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/).

## Source

`https://secure.toronto.ca/opendata/cart/road_restrictions/v3?format=json`, one JSON
document regenerated continuously: about 2,800 restrictions (4 MB) on 2026-10-13, 2,164
construction zones and 657 closures, each with a polyline, start and end time, weekly
working hours, roads affected, direction and expected impact (`maxImpact`, and
`currImpact` for right now). Every entry in that feed was planned work; none had expired.

Feed quirks the parser handles:

- **Not always valid JSON.** A description containing `Water \ Sewer` carried an
  unescaped backslash, which fails a strict parser. On failure the body is repaired by
  doubling any backslash that does not start a valid JSON escape (escapes are matched left
  to right, so an escaped backslash is never split) and parsed again.
- **Truncated fields.** `source`, `workEventType` and `permitType` hold the first few
  characters of an internal JSON table (`{"tabledata":[{"Option":...`). They are ignored.
- **Geometry as text.** `geoPolyline` is a bare list of `[lon,lat]` pairs without the outer
  brackets. Entries without one fall back to their point; anything outside the city is
  skipped.
- **Times as epoch milliseconds in strings.**

## Loading

`transit roads-refresh` runs every 10 minutes (`road_restrictions` Airflow DAG). A response
identical to the latest stored one only updates `last_seen_at`. Otherwise the body is
stored in `raw.road_restriction_responses` (once per distinct body; kept 7 days unless a
row still points at it) and every restriction is upserted into
`normalized.road_restrictions`. A restriction missing from the response gets `removed_at`;
if it comes back, that is cleared. A response listing fewer than half the restrictions
currently in effect (with at least 200 in effect) is treated as partial: rows are updated
but nothing is marked removed, and the refresh is recorded as `dropout`. Every refresh is a
row in `ops.road_restriction_refreshes`, and the pipeline status page and phone alerts
include it.

## Which routes a restriction affects

After each changed response, new or changed restrictions are matched to bus and streetcar
routes (`transit.road_routes`, results in `normalized.road_restriction_routes` per schedule
version). A route's path is drawn stop to stop along its main patterns, which follows the
street except at turns. A restriction lies along a route direction when at least 100 m of
it runs within 35 m of that path; a restriction given as a single point counts within
35 m. The threshold is above the 70 m that any cross street passing straight over the
route has within 35 m of it, so a closure on a side street does not count. On 2026-10-13,
340 of 2,811 restrictions lay along at least one of 149 routes; matching all of them took
0.7 seconds.

## Closures and bus reliability

`ttc_surface_closure_days` lists, per route direction and measured service day, the
restrictions in effect along it: dates covering the service day, listed in the feed at
the time, and working hours that weekday. `ttc_surface_closure_effect` splits each route
direction's weekday reliability sums into days with and without a high-impact
restriction. The report pools route directions with at least 3 weekdays of each and shows
extra wait, bunching and long gaps side by side. Two limits are stated with it: it is a
comparison, not proof of cause (restrictions cluster on busy streets), and a restriction
in place for the whole collection period leaves its routes no days without it.

## On the map

`/api/roads` returns restrictions whose dates cover now. Construction is solid amber and
closures dashed orange, from street level (zoom 12.5); a restriction outside its working
hours right now is faded. Tapping one shows the roads, whether it is in effect now and its
impact, today's hours, the description, the end date, and the bus and streetcar routes it
lies along (tap one to draw the route).
