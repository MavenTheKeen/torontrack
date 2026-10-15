# Events

The **Events page** (`#/events`) lists what is on over the next 14 days: the City of
Toronto's festivals and community events, and concerts, games and shows from Ticketmaster.
The map also shows the city's events for the coming week, and a subway station's panel
lists events within a 10-minute walk (700 m) happening today.

Events: City of Toronto festivals and events calendar. Contains information licensed under
the [Open Government Licence – Toronto](https://www.toronto.ca/city-government/data-research-maps/open-data/open-data-licence/).

## Source and what it covers

Toronto Open Data "Festivals & Events" is the city's own events calendar, published as one
JSON document updated in real time. It lists festivals, farmers' markets, exhibitions,
parades, runs, museum programs and community events: in October 2026, 308 events with dates
in the next 60 days, the Toronto Waterfront Marathon, the Santa Claus parade, Nuit Blanche
and the Cavalcade of Lights among them.

It does **not** list Blue Jays, Maple Leafs or Raptors games and lists only a couple of
arena concerts, the events that move the most riders. League schedule APIs are free to call
but their terms restrict use (MLB's, for example, to individual, non-commercial, non-bulk
use), so they are not used; Ticketmaster (below) covers those events instead.

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

## Concerts, games and shows (Ticketmaster)

Ticketmaster sells tickets for most arenas, stadiums and concert halls in the city. The
first load (2026-10-15) had 326 events over 14 days: 122 concerts, 150 arts and theatre,
17 sports, among them Raptors, Toronto FC, Argonauts and Marlies games. **Maple Leafs
games are not in the Discovery API**: only an add-on sold on game nights appears, and it
is dropped. Its Discovery API
is free with a key from developer.ticketmaster.com (the Consumer Key), stored in
`secrets/ticketmaster_api_key` and mounted into Airflow only. Without a key the page shows city events alone, and each
refresh is recorded as `skipped`.

`transit ticketed-events-refresh` runs every 6 hours (`ticketed_events` Airflow DAG):

- It requests each of the next 14 days separately (events within 30 km of City Hall,
  from Toronto's midnight to the next), following pages; about 56 requests a day of the
  5,000 allowed. The key travels only in the request URL, and errors never include it.
- Ticketmaster lists parking passes, suites, season passes, venue tours and add-ons as
  events; these are dropped by name, as are listings flagged as tests. A game or show
  listed several times at one venue and start time (packages, presales) is kept once,
  under its shortest name. Cancelled events are dropped; postponed and rescheduled ones
  are labelled.
- Timed-entry attractions list every slot (Casa Loma's "Legends of Horror": 104 slots
  over 14 nights), so one name at one venue on one day is kept once, at its first time,
  with the number of sessions.
- A day's query also returns events that started earlier and are still running (a class
  series from August to November); only events starting that day are kept. A start of
  23:59 is a placeholder and is shown as time to be announced.
- The API terms allow caching event content only for reasonable periods to provide the
  service and forbid deriving revenue from it. Each load replaces every upcoming event,
  past events are deleted after a day, and tickets are bought on Ticketmaster: the page
  links to each event's own page.

| Table | Holds |
| --- | --- |
| `normalized.ticketed_events` | Upcoming events: name, segment and genre, start (or date with time to be announced), status, venue, address, coordinates and Ticketmaster link |
| `ops.ticketed_event_refreshes` | Every refresh: status (`loaded`, `skipped`, `failed`), requests, events and skipped listings by reason |

Unlike the other sources, the tests do not use recorded responses: the terms' limit on
storing event content rules out keeping them in a public repository. The hand-built
responses were checked against a real one, and cover the cases the first load showed.

## The Events page

`/api/agenda` returns the 14 days and every event occurrence in them, and the page shows a
strip of the days (dots for how busy each is, an amber dot for a major-venue event), then
the chosen day in time order. `#/events/2026-10-17` opens a given day.

- **Categories:** concerts, sports, arts and theatre, and festivals and community, from
  the city's categories or Ticketmaster's segment and genre; filters show one category or
  only free events.
- **Major venues:** Scotiabank Arena, Rogers Centre, BMO Field, Budweiser Stage and
  Coca-Cola Coliseum. Their events are highlighted with a note that the nearest station
  will be busy before the start and when the event ends.
- **Ongoing events:** an event on 5 or more days of the window (exhibits, markets,
  museum programs, nightly attractions; for Ticketmaster, one name at one venue) is folded
  into an "ongoing" list under each day, so the timeline shows what happens at a time.
- **All-day events:** the city lists them at midnight; they show as "All day", first.
- **Getting there:** each event names the nearest subway station, or a regional station
  if that is more than 3 minutes' walk closer, with the walk at 80 m a minute in a
  straight line (none beyond 20 minutes). **Get there** opens the map's trip planner with
  the venue as the destination.

## On the map

`/api/events` returns events with a date from the start of today to 7 days ahead. Today's
events are solid dark markers, the rest of the week hollow, larger when the city features
the event. Tapping one shows when, where, whether it is free, and a link to its own page.
