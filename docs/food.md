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

## On the site

The map has two kinds, switched with the button beside the search and remembered on the
device: the transit map and the **food map**. The food map hides trains, buses, docks and
the other transit layers and keeps the subway lines for orientation.

The food map shows places to eat from OpenStreetMap: restaurants, cafes, fast food, bars and
pubs, bakeries and dessert shops (`/api/food/places`, about 14,000). Each is matched to the
inspected premises nearest it within 80 m whose name shares at least half the words of the
shorter name; about a third of the city's premises match. Up to zoom 14 places are grouped
into numbered clusters; from zoom 15 each has an icon and colour for its kind (restaurant, café, fast
food, bar or pub, bakery or dessert shop), replaced by amber or red after a conditional
pass or a closure; names appear from zoom 16. A row of kind chips under the search filters
the map: with every kind shown, a tap shows only that kind, and further taps add or remove
kinds. The filter panel shows or hides each kind as well, can limit
the map to clean records (a latest Pass and no significant or crucial infraction in the
last two years) or to long-running places (below), and can add the inspected premises matched to no place: grocers,
institutional kitchens and places missing from OpenStreetMap. The city's list gives no type
of premises, so they are kept apart. Search lists food premises first. The data has no
ratings or reviews; no open source of them exists for Toronto.

On the transit map, the **Food inspections** layer (off until switched on) shows, from zoom 15, every
premises in the newest load, coloured by its latest result: Pass, Conditional Pass, Closed
or another status. Premises that have left the city's file are not drawn; they have closed
or changed hands. The layer's data (`/api/food`, about 20,000 premises) is loaded the first
time the layer is switched on and cached on the server for an hour.

Typing three or more letters in the map search also lists up to four premises whose name
contains every word typed, names starting with the text first, then the nearest. Each shows
its latest result and date.

Tapping a premises opens its panel: the latest inspection with its date, result and
infractions (most severe first, with any court outcome and fine), the earlier inspections
with their results and number of infractions, up to ten in all, and directions to it.

## Reading the results

A Conditional Pass or Closed result is a point-in-time finding; premises are re-inspected,
and a closed premises reopens once it passes. Results shown on the site therefore always
carry their inspection date, with the latest inspection first. Names and addresses the city
publishes in capitals are shown in normal case (`display`); acronyms such as BBQ, Roman
numerals and postal codes stay in capitals.

## Business licences

`transit food-licences-refresh` loads the City of Toronto business licences
(`municipal-licensing-and-standards-business-licences-and-permits`, Open Government
Licence – Toronto) in the categories eating or drinking establishment, expanded eating or
drinking establishment and take-out or retail food establishment, current and cancelled,
into `normalized.food_licences`. It runs after the inspections load in the same daily job
and is skipped when the export has not changed. Only the licence number, category,
operating name, street address and dates are kept; holder names and phone numbers are not.

A licence belongs to its holder, so a new owner takes out a new one. A premises is shown as
licensed since the earliest licence at the same street address (without unit or postal
code) whose operating name shares at least half the words of the shorter name, so a change
of owner that keeps the name keeps the year (`food_licences.since`). About half of the
inspected premises match a licence. The premises card shows the year, and the food map can
show only places licensed there for 20 years or more, labelled with the year.
