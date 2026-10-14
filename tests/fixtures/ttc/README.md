# TTC fixture

`alerts_subway.pb` is a **captured test fixture, not live data**: the complete,
unmodified binary response from `https://gtfsrt.ttc.ca/alerts/subway?format=binary`
fetched on 2026-10-09 (feed timestamp `2026-10-09T13:16:39Z`). It contains three
alerts, including a planned closure whose effect (`REDUCED_SERVICE`) contradicts its
text ("no subway service"). CI does not contact the API.

Other TTC test inputs are constructed in `tests/ttc_helpers.py` from real stop IDs and
platform names with made-up schedules and predictions.

Data: Toronto Transit Commission.
