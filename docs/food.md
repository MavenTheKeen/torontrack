# Food inspections

Toronto Public Health inspects every food premises in the city (restaurants, cafes, take-out
counters, grocers, institutional kitchens) and publishes the results as DineSafe on the
City of Toronto Open Data portal. The project loads them daily
(`transit.food_inspections`).

Contains information licensed under the Open Government Licence – Toronto.

## Source

The dataset `dinesafe` is one table covering about the last two to three years, refreshed
daily. Each row is one infraction found at an inspection, or one row for an inspection
with none. The project reads the datastore copy (`/datastore/dump/<resource id>`), which
carried inspections up to the previous day when checked.

In the export of 8 November 2026: 126,286 rows, 20,084 premises, 84,834 inspections from
10 November 2023 to 7 November 2026, and 79,618 infractions. Statuses were Pass (79,674
inspections), Conditional Pass (5,073), Closed (86) and Temporarily Not Operating (1).
Every row had coordinates inside the city.

## Loading

`food-inspections-refresh` runs daily at 10:10 UTC (scheduler job and Airflow DAG
`food_inspections`). The export is streamed to a temporary file and hashed; an unchanged
export is not reloaded. Otherwise, in one transaction:

- premises are inserted or updated (name, address, coordinates, last seen);
- inspections are inserted or updated by premises and date, with their status;
- the infractions of every inspection in the export are replaced.

Inspections that leave the city's file are kept, so the history grows past the city's
window. An export listing fewer than 5,000 premises is treated as truncated and not loaded.

Cleaning: empty address parts, exported as the word `None`, are removed; severities are
stored as `minor`, `significant`, `crucial` or `not applicable`; the two spellings of a
fined conviction are merged. Rows without a premises ID, date or status, or with
coordinates outside the city, are skipped and counted.

| Table | Holds |
| --- | --- |
| `normalized.food_premises` | City premises ID, name, address, coordinates, last load that listed it |
| `normalized.food_inspections` | One row per premises and inspection date, with the status |
| `normalized.food_infractions` | Each infraction: wording, category, severity, and court outcome, date and fine if charged |
| `ops.food_inspection_refreshes` | Each load: status, export hash, counts, error |

The pipeline page and phone alerts report the load as delayed after two days without a
successful run.

## Reading the results

A Conditional Pass or Closed result is a point-in-time finding; premises are re-inspected,
and a closed premises reopens once it passes. Results shown on the site therefore always
carry their inspection date, with the latest inspection first.
