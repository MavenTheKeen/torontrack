# Source verification and attribution

Inspected on **2026-10-09** using real HTTP responses, before implementing the parser.

## Discovery and feeds

The [official GBFS systems registry](https://github.com/MobilityData/gbfs/blob/master/systems.csv)
lists Bike Share Toronto's discovery URL as:

<https://toronto.publicbikesystem.net/customer/gbfs/v3.0/gbfs.json>

The endpoint returned `version: "3.0"`. Its `data.feeds` array advertised these
names (discover their URLs at runtime; the filenames are not an API contract):

| Feed | Inspected discovery URL |
| --- | --- |
| `gbfs_versions` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/gbfs_versions |
| `station_information` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/station_information |
| `station_status` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/station_status |
| `system_information` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/system_information |
| `geofencing_zones` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/geofencing_zones |
| `system_pricing_plans` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/system_pricing_plans |
| `system_regions` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/system_regions |
| `vehicle_types` | https://toronto.publicbikesystem.net/customer/gbfs/v3.0/vehicle_types |

`gbfs_versions` advertised 1.1, 2.3, and 3.0. This project deliberately targets
3.0. The [City dataset page](https://open.toronto.ca/dataset/bike-share-toronto/)
also links GBFS, but its downloadable discovery JSON is a **2019 snapshot** using
old URLs. It must not be mistaken for today's live discovery response.

## Observed response shape

Responses have `last_updated`, `ttl`, `version`, and `data`. Both station feeds
contain `data.stations`. Metadata includes string station IDs, localized names
such as `[{"text":"Fort York  Blvd / Capreol Ct","language":"en"}]`, `lat`,
`lon`, and `capacity`. Status uses `num_vehicles_available`,
`num_vehicles_disabled`, `num_docks_available`, `num_docks_disabled`, native JSON
booleans for the three service flags, and RFC3339 `last_reported` strings.

For example, the real 7000 response at feed publication time
`2026-10-09T09:39:55Z` reported 42 available vehicles, 3 available docks, and 2
disabled vehicles, with metadata capacity 47. This is an observed example,
not a permanent station value or a fixture advertised as live data.

The [GBFS 3.0 reference](https://www.gbfs.org/documentation/reference/)
distinguishes publisher knowledge (`last_updated`) from station communication
(`last_reported`). The project's HTTP completion time is a third timestamp.
Counts alone do not establish rental/return availability: the service flags
also matter. Capacity is optional, and disabled equipment can explain differences
between capacity and available counts. Unknown extension fields remain in raw
JSON; they do not automatically become trusted analytical columns.

## Attribution and usage

Data source: [Bike Share Toronto](https://bikesharetoronto.com/), operated by the
Toronto Parking Authority, through its public GBFS feed.

The City dataset page links the
[Open Government Licence – Toronto](https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/).
Include this attribution in the dashboard and documentation:

> Contains information licensed under the Open Government Licence – Toronto.

The licence permits reuse with attribution and does not imply endorsement.
This project is independent and does not use official logos. There is a metadata
discrepancy worth preserving: the inspected live `system_information` response
did **not** declare `license_id`, `license_url`, or an attribution string; the
[City CKAN API](https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/package_show?id=bike-share-toronto)
also returned `license_title: "License not specified"`. The City webpage's licence
link is the basis for the attribution above; this project does not claim that the
live feed itself declares that licence or CC-BY-4.0.

No numerical request quota was found in these official source pages or inspected
response headers. This means the limit is **unknown**, not unlimited. The inspected
HTTP response used `Cache-Control: public, s-maxage=10, max-age=10`; discovery
`ttl` was 10 and one status response's `ttl` was 6. The application polls about
every 15 minutes, uses bounded retries, and respects server retry instructions.
The feed contact published by the source was `mobility-data-client@lyft.com`.

## Data limitations

GBFS provides present availability, not a historical availability archive.
History starts with actual collections. Gaps remain gaps; the dashboard must not
infer trips or exact empty/full duration from snapshots. A successful request can
still contain stale station reports, inactive stations, or inconsistent metadata.
Source changes and short-lived outages are possible. The dashboard presents
sampled availability with freshness and coverage, not a promise of availability
at arrival.
