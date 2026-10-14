# Weather fixture

`climate_hourly.json` is a captured response, not live data. It was fetched on
2026-10-13 from Environment and Climate Change Canada's MSC GeoMet API:
`https://api.weather.gc.ca/collections/climate-hourly/items` for station 6158355
(Toronto City), local hours 14:00 to 17:00 on 2026-10-10. Only the `links` array was
emptied and whitespace reformatted; the four hourly records are unchanged. Tests mutate
copies in memory.

Contains information licensed under the
[Open Government Licence – Canada](https://open.canada.ca/en/open-government-licence-canada).
