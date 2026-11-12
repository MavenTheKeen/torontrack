# Wards and 311 service requests

`transit neighbourhood-refresh` loads two City of Toronto datasets under the Open
Government Licence – Toronto:

- **City wards** (`city-wards`): the 25 ward boundaries, replaced on each load
  (`normalized.city_wards`).
- **311 service requests** (`311-service-requests-customer-initiated`): one zipped CSV per
  year, refreshed about monthly. A request has no address, only its ward, the first three
  characters of its postal code and sometimes an intersection. Requests are kept only as
  daily counts per ward, request type and division (`normalized.service_requests_daily`);
  no row identifies a caller or a household. Years from 2021, the first full year on the
  25-ward model in this format, are loaded. The current year is fetched on every run and
  skipped when its SHA-256 matches the last load (`ops.service_request_files`); earlier
  years are fetched once. Files differ in encoding by year (the 2026 file is
  Windows-1252), so each line is read as UTF-8 and, failing that, as Windows-1252.

The scheduler runs the load daily at 10:50 UTC (`service_requests`).

## My street

`GET /api/street?at=lat,lon` returns, for a point:

- its next curbside collections ([curbside collection](waste.md));
- road work and closures in effect within 500 m, nearest first, measured to the
  restriction's path ([road restrictions](roads.md));
- its ward and the ward's five most common 311 request types in the 30 days up to the
  newest day loaded, each with the city-wide count of that type.

An address or place card on the map shows all three, with a Save button. A saved street is
kept in the browser only, with its coordinates rounded to three decimal places (about
100 m), and appears on the Status page with its next collection, the number of road works
nearby and the ward's most common request.
