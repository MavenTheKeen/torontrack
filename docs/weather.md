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
