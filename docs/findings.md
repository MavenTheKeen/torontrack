# Findings

The Findings page (`#/findings`, `GET /api/findings`; charts draw in as they scroll into
view unless motion is reduced) compares the sources collected here
and states what they show, each with its method, period and sources. Findings are
computed from the database by `transit.findings` and cached for an hour. A finding whose
data is missing or too thin is left out rather than shown with a weak number, and the
wording follows the result: a small difference is reported as no clear difference.

Weather for every finding comes from ECCC's hourly observations at Toronto City
(6158355), summed into Toronto calendar days; a day counts only with at least 20 reported
hours of temperature and precipitation. The hourly history was backfilled from January
2021 for this purpose ([weather](weather.md)).

| Finding | Data | Method |
|---|---|---|
| Rain and Bike Share | Ridership files from 2024, daily weather | Wet days (1 mm or more) against dry days within the same 5 °C band of the day's high and the same kind of day (weekday or weekend), weighted by wet days. Ridership growth over the period is not removed. |
| Snow and subway delays | TTC subway delay log from 2025, December to March | Delays and delay minutes per day on snowy days (2 mm or more fell at or below 0.5 °C) against other winter days. |
| Causes of subway delay | TTC subway delay log | Delay minutes by the TTC's cause code and description; the eight largest. |
| Potholes and freeze-thaw | 311 requests from 2021, daily weather | Pothole requests per month against the days in the previous month with a low below and a high above 0 °C; Pearson's r. A request is not a measured pothole. |
| Beaches and rain | Beach samples from 2021, daily weather | Share of beach days over Toronto's E. coli standard (100 per 100 mL, geometric mean of sites) by the previous day's rain downtown. |
| Inspection results and age | DineSafe inspections of the last two years, business licences | Share of inspections not passed by years licensed at the address. Places that closed are missing, so older places are survivors; the result is a difference, not a cause. |
