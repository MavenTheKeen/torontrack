# GBFS fixtures

These are **captured test fixtures, not live data or historical coverage**. They
were fetched from Bike Share Toronto on 2026-10-09. CI does not contact the API.

- `discovery.json`: complete response from
  `https://toronto.publicbikesystem.net/customer/gbfs/v3.0/gbfs.json`, source
  publication time `2026-10-09T09:39:34Z`.
- `station_information.json`: only stations 7000 and 7001 from the discovered
  `station_information` URL, publication time `2026-10-09T09:39:47Z`.
- `station_status.json`: the same two stations from the discovered
  `station_status` URL, publication time `2026-10-09T09:39:47Z`.

The station records and envelope values are unchanged; the station arrays were
reduced and JSON whitespace reformatted. Tests make clearly artificial mutations
to these fixtures in memory to exercise failures. Fixture timestamps intentionally
remain fixed. Do not import these fixtures into a dashboard's live database.

Source: [Bike Share Toronto](https://bikesharetoronto.com/).
Contains information licensed under the
[Open Government Licence – Toronto](https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/).
See [source verification](../../docs/source.md) for licensing metadata limitations.
