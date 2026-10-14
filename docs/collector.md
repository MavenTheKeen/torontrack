# Collector server

The project can run split across two machines:

- an always-on **collector server** runs PostgreSQL and the live collectors only;
- a **development machine** runs everything else (site, dashboard, alerts, Airflow, dbt,
  the trip planner and the non-live loaders) against a local PostgreSQL that receives the
  collected tables by logical replication.

Live sources cannot be fetched again later, so they are collected on the server, which
stays on. Every other source (schedules for regional trains, ridership files, events,
weather, the TTC delay log) can be loaded at any time, so those loads run on the
development machine.

```text
collector server                          development machine
────────────────                          ───────────────────
ttc-collector ──┐                         web, dashboard, alerts
bikeshare-collector ──> PostgreSQL ──SSH──> PostgreSQL <── Airflow (dbt, loaders)
roads-collector ┘      (publication)      (subscription)   planner
ttc-schedule ───┘
```

## What runs where

| Collector server (`deploy/collector/compose.yaml`) | Schedule |
| --- | --- |
| `ttc-collector`: TTC realtime feeds | predictions and positions every 10 s, alerts every 30 s |
| `bikeshare-collector`: Bike Share availability | every 15 min |
| `roads-collector`: City of Toronto road restrictions | every 10 min |
| `ttc-schedule`: TTC static GTFS, loaded when changed | every 6 h |

On the development machine, the Airflow DAGs that would duplicate this work are paused
(`toronto_bikeshare_reliability`, `road_restrictions`, `ttc_static_gtfs`), the TTC
collector is not started, and `dbt_build` builds the dbt models every 15 minutes in place
of the Bike Share DAG.

Each server schedule is a shell loop aligned to the clock. A failed run is recorded by
the command in the `ops` tables, as under Airflow, and the loop continues.

## Replication

`deploy/collector/publication.sql` publishes the 39 tables the collectors write. Each
table is written on one side only, so replicated rows never conflict with local writes.
Raw TTC snapshots (`raw.ttc_realtime_snapshots`) are not published: they are kept only
for replay, and replicating them would transfer about 1 GB a day.

`normalized.ttc_surface_passages` has no primary key, so it is published with
`REPLICA IDENTITY FULL`. The subscriber locates rows for retention deletes through its
`passed_at` index.

The server's PostgreSQL listens on `127.0.0.1` only. The development machine reaches it
through the `server-tunnel` service (Compose profile `collector`), an SSH port forward
that reconnects automatically. While the development machine is off, the replication
slot on the server retains unsent changes, up to `max_slot_wal_keep_size` (20 GB). The
subscriber catches up when it reconnects. If the slot exceeds that limit it is
invalidated, and the subscription must be recreated as in setup step 5.

Measured on setup (October 2026): the initial copy of the 39 tables took under a minute,
and replication lag afterwards was below one second.

## Setup

On the development machine, with the stack built:

1. **Load the app image onto the server**, so the server needs no source code:

   ```powershell
   docker save toronto-transit-app:local | ssh -p PORT user@server docker load
   ```

2. **On the server**, create a directory with `deploy/collector/compose.yaml`,
   `deploy/collector/publication.sql` and a `.env` file containing a random
   `POSTGRES_PASSWORD`. Start it and create the publication:

   ```bash
   docker compose up -d --wait
   docker compose exec -T postgres psql -U transit -d transit -f - < publication.sql
   ```

3. **On the development machine**, set `COLLECTOR_SSH`, `COLLECTOR_SSH_PORT`,
   `COLLECTOR_SSH_KEY`, `COLLECTOR_KNOWN_HOSTS` and `COLLECTOR_POSTGRES_PASSWORD` in `.env`
   (see `.env.example`) and start the tunnel:

   ```powershell
   docker compose --profile collector up -d server-tunnel
   ```

4. **Pause the duplicate DAGs** and unpause `dbt_build`, as described above. Do not start
   `ttc-collector` on the development machine.

5. **Subscribe.** The published tables must be empty locally before the initial copy;
   `raw.ttc_realtime_snapshots` is truncated with them because it references the TTC
   schedule tables:

   ```sql
   TRUNCATE <published tables>, raw.ttc_realtime_snapshots;
   CREATE SUBSCRIPTION collected
     CONNECTION 'host=server-tunnel port=5432 dbname=transit user=transit password=...'
     PUBLICATION collected WITH (copy_data = true);
   ```

   `SELECT srsubstate, count(*) FROM pg_subscription_rel GROUP BY 1` shows every table
   as `r` (ready) once the initial copy is complete.

## Schema changes

Migrations must run on both databases, server first. `init-db` runs when the
collector stack starts, so loading a new app image onto the server and restarting it
applies them there. A new table that the collectors write must also be added to the
publication (`ALTER PUBLICATION collected ADD TABLE ...`), followed by
`ALTER SUBSCRIPTION collected REFRESH PUBLICATION` on the development machine.

## Checking it

```sql
-- Development machine: replication lag.
SELECT now() - latest_end_time FROM pg_stat_subscription WHERE subname = 'collected';
-- Server: changes retained for the development machine.
SELECT pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))
FROM pg_replication_slots WHERE slot_name = 'collected';
```

The pipeline status page and phone alerts run on the development machine, so they
report collector problems only while it is on.
