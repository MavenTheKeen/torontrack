# Skating and swimming

`transit recreation-refresh` loads two City of Toronto datasets under the Open Government
Licence – Toronto, replacing the previous load:

- **Parks and recreation facilities** (`parks-and-recreation-facilities`): each location's
  name, address, city page and position (`normalized.rec_facilities`).
- **Registered programs and drop-in courses** (`registered-programs-and-drop-in-courses-offering`),
  its Drop-in table: one row per session and date for the coming weeks. Sessions in the
  sections Skate - Drop-In (leisure skates, shinny) and Swim - Drop-In (leisure, lane and
  other swims) are kept with their title, times and ages (`normalized.rec_sessions`).

The city refreshes both weekly; the scheduler runs the load daily at 11:00 UTC
(`recreation`). `GET /api/recreation` returns every location with a session today, in
Toronto time, with its sessions in order.

## City map

The map's third kind, City, shows those locations: blue for skating and teal for swimming,
ringed in green while a session is on and hollow once the day's last session has ended.
Names and the next session appear from zoom 13. Chips under the search show skating,
swimming or only places with a session on now, and a place's card lists the day's
sessions with their ages and a link to its page on toronto.ca. Sessions are shown as
published; closures announced elsewhere are not reflected.

## Beaches

`transit beaches-refresh` runs after the drop-in load in the same daily job. It loads
Toronto Public Health's beach water quality samples (`toronto-beaches-water-quality`):
every E. coli count since 2007, at several sites per beach, in `normalized.beach_samples`,
replaced on each load. A beach's result for a day is the geometric mean of its sites (a
count of 0 is taken as 1). Toronto posts a beach as unsafe above 100 E. coli per 100 mL,
stricter than the 200 used by Ontario and Canada; the site applies the same threshold.

`GET /api/beaches` returns each beach with its latest seven sampling days, and whether
the season is on (a sample within the last three days). On the city map a beach is green
or red by its latest result in season and hollow grey out of season; its card lists the
recent results. The same samples feed the beach and rain finding ([findings](findings.md)).

## Events today

The city map also shows the day's events from the Events page's agenda (`/api/agenda`:
city festivals and events and Ticketmaster events). Events at a major venue, an arena or
stadium, are drawn larger with a gold ring and named at every zoom; others are named from
zoom 15. A "Today" chip opens the day's major and featured events, and an event's card
gives its time, venue, nearest station and a link to its page.

## CaféTO patios

`transit patios-refresh` runs last in the same job and loads the CaféTO patio locations
(`cafeto-curb-lane-parklet-cafe-locations`, refreshed monthly): operator, address, kind of
patio (curb lane, sidewalk, parklet, private property) and position, replaced on each load
(`normalized.cafeto_patios`). `GET /api/patios` lists them; the city map shows them from
zoom 13 behind a Patios chip, with names from zoom 16.
