# Schema migrations

`transit init-db` applies, in one transaction:

1. The frozen baselines `transit/bikeshare/schema.sql` and `transit/ttc/schema.sql`
   (recorded as `0000_baseline_*`). Do not edit them; they describe the schema as it
   was before migrations existed.
2. Every `NNNN_description.sql` file in this folder, in name order.

Each step runs once and is recorded in `ops.schema_migrations`. A failed step rolls back
the whole run. A migration that moves data should check its result (row counts,
reconstruction) and raise before dropping anything, and needs a test in
`tests/test_migrations.py` that runs it against a database built at the previous schema.

Before migrating a live database: back up, stop writers that use the affected tables,
deploy the new code, then run `init-db`.
