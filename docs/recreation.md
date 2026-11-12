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
