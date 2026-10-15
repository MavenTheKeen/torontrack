# Local scheduling

Airflow uses the official `apache/airflow:3.3.2-python3.12` image and the
`standalone` command with `LocalExecutor`. That is the supported small local
development setup, not a production process supervisor. PostgreSQL stores Airflow
metadata in the separate `airflow_metadata` database. There is no Redis, Celery,
Docker socket mount, or worker service.

The app and dbt run in separate virtual environments inside the Airflow container;
their locked dependencies do not modify Airflow's Python installation. DAG tasks
execute absolute CLI paths with argument lists and inherit credentials through
environment variables.

```powershell
docker compose --profile airflow build airflow
docker compose --profile airflow up -d airflow
docker compose --profile airflow logs --tail 80 airflow
docker compose exec airflow airflow dags list-import-errors
docker compose exec airflow airflow dags unpause toronto_bikeshare_reliability
```

Open <http://localhost:8080>. The DAG starts paused so startup and CI do not make
unrequested live API calls. After unpausing, it collects approximately every 15
minutes. A manual trigger is also available:

```powershell
docker compose exec airflow airflow dags trigger toronto_bikeshare_reliability
```

`collect -> replay -> transform` records live raw JSON, replays it without HTTP,
then builds/tests dbt models. The replay task provides an independently rerunnable
normalization boundary. Collection already normalizes for the independent CLI
workflow, so scheduled replay is intentionally redundant and idempotent.

`catchup=False`, one active DAG run, one task at a time, and the PostgreSQL
collection lock prevent overlapping collection writes. Each task has two retries,
2-minute initial exponential retry delay capped at 5 minutes, and a 12-minute
execution timeout. An HTTP Retry-After deadline is stored in PostgreSQL and also
applies to later Airflow retries. Failed tasks remain failed visibly; no empty
dataset is substituted.

Other DAGs, each also paused until unpaused, include:

| DAG | Schedule (UTC) | Runs |
| --- | --- | --- |
| `ttc_static_gtfs` | 09:00 | `ttc-gtfs-refresh`: loads TTC's static schedule as a new version only if the zip changed |
| `ttc_official_delays` | 10:00 | `ttc-delays-refresh`: loads TTC's official delay log when a new month is published |
| `weather_hourly` | hourly at :20 | `weather-refresh`: loads Environment Canada's hourly weather, re-reading two days so revisions and missed runs are caught up ([weather](weather.md)) |
| `open_data_export` | 09:30 | `export`: writes completed days as [open-data files](datasets.md), catching up any missed days |
| `ticketed_events` | every 6 h at :20 | `ticketed-events-refresh`: loads Ticketmaster concerts, games and shows for the next 14 days ([events](events.md#concerts-games-and-shows-ticketmaster)) |

The collection ID hashes DAG ID + run ID. Clearing a task reuses stored raw data.
The collected timestamp is the actual HTTP acquisition time, never the logical
date. Do not backfill a live feed to try to create historical observations.

After fixing a parser or SQL issue, clear only the appropriate failed task and
downstream tasks in the Airflow UI. A failed dbt build can rerun without collecting
again. Outside Airflow, use `collector replay --collection-id ...` and the dbt
service.

## Health and recovery

The container health check inspects Airflow's metadata database, scheduler, DAG
processor, and triggerer status from `/api/v2/monitor/health`; HTTP 200 alone does
not prove that they are healthy. Pipeline collection failures, freshness checks,
and recorded dbt tests also appear in the Streamlit dashboard.

Standalone does not restart an individual child process that dies. If its health
check fails, inspect logs then run `docker compose restart airflow`. Docker's
restart policy restarts exited containers, not merely unhealthy containers.

The scheduler is one such process: when PostgreSQL restarts, it exits on the dropped
connection and scheduled runs stop while the container keeps running. Compose
therefore restarts Airflow whenever it restarts or recreates PostgreSQL
(`depends_on: restart: true`). If PostgreSQL restarts any other way (a crash, or
Docker restarting it), restart Airflow by hand; the [pipeline status
page](site.md) shows Bike Share collection as delayed within 15 minutes.

The UI is intentionally local: the host port binds to `127.0.0.1`, and the built-in
Simple Auth Manager allows all local visitors to act as admins. This is not a
public deployment configuration. DAG code is baked into the image; rebuild after
changes. Logs and config/state use separate named volumes, so they do not hide
updated DAG files.

## Testing

GitHub Actions runs the DAG import and structure tests in a separate Linux job with
Airflow's official constraints, and a Compose job builds every image and waits for
the dashboard and Airflow health checks with the DAG still paused. Scheduled runs
are exercised locally with Docker Desktop (WSL 2 backend) on Windows 11.

Unpausing with `catchup=False` immediately queues the most recent missed 15-minute
slot, then continues on schedule. That first run collects at unpause time, because
collection time is always the actual HTTP acquisition time.

Official references: [Quick start](https://airflow.apache.org/docs/apache-airflow/3.3.2/start.html),
[prerequisites](https://airflow.apache.org/docs/apache-airflow/3.3.2/installation/prerequisites.html),
[database setup](https://airflow.apache.org/docs/apache-airflow/3.3.2/howto/set-up-database.html),
[Simple Auth Manager](https://airflow.apache.org/docs/apache-airflow/3.3.2/core-concepts/auth-manager/simple/index.html),
[health checks](https://airflow.apache.org/docs/apache-airflow/3.3.2/administration-and-deployment/logging-monitoring/check-health.html).
