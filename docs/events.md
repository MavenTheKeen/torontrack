# Events

The map shows the City of Toronto's festivals and events for the coming week, and a subway
station's panel lists events within a 10-minute walk (700 m) happening today.

Events: City of Toronto festivals and events calendar. Contains information licensed under
the [Open Government Licence – Toronto](https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/).

## Source and what it covers

Toronto Open Data "Festivals & Events" is the city's own events calendar, published as one
JSON document updated in real time. It lists festivals, farmers' markets, exhibitions,
parades, runs, museum programs and community events: in October 2026, 308 events with dates
in the next 60 days, the Toronto Waterfront Marathon, the Santa Claus parade, Nuit Blanche
and the Cavalcade of Lights among them.

It does **not** list Blue Jays, Maple Leafs or Raptors games and lists only a couple of
arena concerts, the events that move the most riders. So the site shows it as context and
does not measure crowding from it. League schedule APIs are free to call but their terms
restrict use (MLB's, for example, to individual, non-commercial, non-bulk use), so they are
not used.

## Loading

`transit events-refresh` runs daily (`city_events` Airflow DAG). The feed is about 225 MB,
which is over a gigabyte in memory if parsed whole, so it is streamed to a temporary file
and read one event at a time with `ijson`. Its SHA-256 is compared with the last load and
an unchanged feed is not read again; the body itself is not stored.

What the feed contains, and how the loader handles it:

- **Each event is repeated once per date.** The copies have different IDs but identical
  content and the same full list of dates: 3,703 records for 308 events in the 60-day
  window, up to 100 copies of one market. Copies with the same name and location are merged
  under an ID derived from those two, which is also stable across loads.
- Only approved events (`event_status`) with a location inside the city are kept; the
  location's coordinates are a JSON string inside the location record.
- Websites are often given without a scheme (`assembly.ca`) and get `https://`.

| Table | Holds |
| --- | --- |
| `normalized.city_events` | Name, categories, featured, free, website, venue, address and coordinates |
| `normalized.city_event_occurrences` | Each date of each event. Dates from now on are replaced on every load, so a cancelled date disappears; past dates are kept. |
| `ops.city_event_refreshes` | Every load: feed hash, events, dates, and records skipped by reason |

## On the map

`/api/events` returns events with a date from the start of today to 7 days ahead. Today's
events are solid dark markers, the rest of the week hollow, larger when the city features
the event. Tapping one shows when, where, whether it is free, and a link to its own page.
