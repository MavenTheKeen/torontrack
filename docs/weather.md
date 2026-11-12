# Weather

Hourly observations from Environment and Climate Change Canada (ECCC), so subway gaps
and Bike Share use can be compared with the weather at the same hour.

- **Source:** the `climate-hourly` collection of ECCC's
  [MSC GeoMet API](https://api.weather.gc.ca/collections/climate-hourly), station
  6158355, Toronto City (University of Toronto, downtown). Temperature, dew point,
  humidity, precipitation and station pressure are reported there; wind, visibility and
  a weather description are not.
- **Delay:** each hour appears several hours after it ends (about 10 hours when checked
  on 2026-10-13) and can be revised after quality control.

## Loading

`transit weather-refresh` runs hourly in Airflow (`weather_hourly`, at 20 past). Each run
re-reads the two days before the latest stored hour and overwrites them, so revisions
are picked up and a missed run catches up. Unlike the realtime transit feeds, ECCC keeps
history, so `--since 2026-10-09` backfills the whole collection period.

| Table | Holds |
| --- | --- |
| `raw.weather_responses` | Each API response; responses no hour points to any more are deleted after 7 days |
| `normalized.weather_hourly` | One row per station and hour (UTC), with ECCC's data-quality flags |
| `ops.weather_refreshes` | Every load attempt: status, hours, latest observation |
| `staging.stg_weather_hourly` | The same hours keyed by TTC service day and hour, with `conditions` (`dry`, `rain` or `snow`) |

The API filters on the station's local standard time, which ignores daylight saving;
stored times come from the response's UTC field, so they are exact.

## Licence

Contains information licensed under the
[Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).

## Air quality

`transit air-quality-refresh` runs after the weather in the same hourly job. It reads the
Air Quality Health Index from ECCC's MSC GeoMet API for the stations in Toronto's bounding
box (Downtown, East, North, West and a Toronto-wide value): observations of the last two
days are upserted into `normalized.aqhi_observations`, which grows into a history, and
each station's newest forecast replaces its previous one in `normalized.aqhi_forecasts`.
`GET /api/air` returns each station's latest reading within six hours, Downtown first,
and Downtown's forecast, with ECCC's risk categories (1 to 3 low, 4 to 6 moderate, 7 to 10
high, above 10 very high). The Status page shows Downtown's reading and the highest
forecast for the rest of today and for tomorrow.

## Weather map

`transit weather-now-refresh` runs every six minutes (`weather_now`). From ECCC's MSC
GeoMet it reads the city page for Toronto (`citypageweather-realtime`, item `on-143`:
current conditions, the next 24 hourly forecasts, the daily forecasts and any warnings),
kept as a plain summary in `normalized.weather_citypage`, and the radar: the WMS layers
`RADAR_1KM_RRAI` (rain rate) and `RADAR_1KM_RSNO` (snow rate), whose time dimension steps
every six minutes. Each new frame of the last hour is fetched once as a PNG of the Toronto
area in Web Mercator and stored in `normalized.radar_frames`; frames older than two hours
are dropped. Storing the images lets the site serve them itself, so browsers never
contact ECCC and the Content Security Policy keeps no outside origins.

`GET /api/weather` returns the summary and the frame times; `GET
/api/radar/{rain|snow}/{epoch}.png` returns one frame, cacheable for good since a frame
never changes. The map's fourth kind, Weather, plays the last hour of radar as a loop
(still for visitors who reduce motion), shows the Air Quality Health Index at each
station, and opens a card with the conditions, the next twelve hours, warnings, the
daily forecast and what the findings say the coming weather tends to do: fewer Bike Share
trips on wet days, longer subway delays on snowy days, beaches failing after heavy rain in
summer and pothole reports after freeze-thaw ([findings](findings.md)).
