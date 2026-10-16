# Running it locally

## Requirements

Docker with Compose v2 (on Windows, Docker Desktop with WSL2; see
[Windows setup](setup-windows.md)) and about 6–8 GB of RAM, plus 5 GB while the trip
planner rebuilds its graph. Passwords and keys are files in `secrets/`, created by
`deploy/make_secrets.sh` (run it in Git Bash on Windows) and ignored by Git
([secrets](deploy.md#secrets)).

## Full setup

```powershell
Copy-Item .env.example .env
sh deploy/make_secrets.sh
docker compose --profile tools build
docker compose up -d --wait postgres
docker compose run --rm init-db
docker compose up -d ttc-collector web alerts dashboard
docker compose run --rm collector collect
docker compose run --rm dbt
docker compose run --rm ttc-collector regional-refresh
docker compose run --rm ttc-collector bikeshare-trips-refresh
docker compose run --rm ttc-collector events-refresh
docker compose --profile tools run --rm map-tiles
docker compose --profile planner up -d
```

The site is served at <http://localhost:8000> and the Bike Share dashboard at
<http://localhost:8501>.

The TTC collector loads the static schedule on first start, then polls subway
predictions and vehicle positions every 10 seconds and alerts every 30.
`bikeshare-trips-refresh` loads every Bike Share trip since 2024 (three files of about
200 MB). `map-tiles` builds the basemap and the bike lane, station entrance, parking and
bike parking layers in about two minutes ([street map](map.md)). The `planner` profile
downloads its inputs, then builds and serves the routing graph in a few minutes
([trip planner](planner.md)).

## Scheduled jobs

Everything that is not a continuous collector runs on a schedule: Bike Share collection
and dbt builds, schedule refreshes, ridership, roads, events, weather, the TTC delay log,
open-data exports and planner downloads. There are two ways to run them.

The built-in scheduler (`transit.scheduler`, compose profile `scheduler`) is what
production uses. It runs the same commands on the same schedules as the Airflow DAGs and
idles at about 60 MB, against about 1.1 GB for Airflow:

```powershell
docker compose --profile scheduler up -d
```

Airflow runs the same jobs as DAGs, with its UI at <http://localhost:8080>
([Airflow operation](airflow.md)):

```powershell
docker compose --profile airflow build airflow
docker compose --profile airflow up -d airflow
foreach ($dag in "toronto_bikeshare_reliability", "ttc_static_gtfs", "open_data_export",
    "ttc_official_delays", "weather_hourly", "regional_schedule", "road_restrictions",
    "bikeshare_trips", "city_events", "ticketed_events", "planner_data") {
  docker compose exec airflow airflow dags unpause $dag
}
```

Run one or the other, not both. All ports bind to localhost. To stop everything without
deleting collected history, run `docker compose --profile airflow --profile scheduler
--profile planner down`; adding `--volumes` deletes all collected data.

## Tests

Tests use recorded real responses and controlled mutations, never a live API. GitHub
Actions runs the PostgreSQL and dbt suite, an Airflow DAG import test in the Airflow
image, and container startup checks.

Integration tests **clear project tables** in a database whose name must end in
`_test`; they never touch the live `transit` database. With Python 3.12, uv 0.9.5 and
the Compose PostgreSQL running:

```powershell
uv sync --locked
uv sync --project dbt --locked
docker compose exec postgres createdb -U transit transit_test
$env:TEST_DATABASE_URL="postgresql://transit:$(Get-Content secrets/postgres_password)@127.0.0.1:5432/transit_test"
$env:DBT_EXECUTABLE="$PWD/dbt/.venv/Scripts/dbt.exe"
uv run --locked pytest -q
uv run --locked ruff check src tests dashboard scripts airflow
```

Use `127.0.0.1`, not `localhost`: on Windows, `localhost` tries IPv6 first and
PostgreSQL is published on IPv4 only. On Linux and macOS, use `export NAME=value` and
`DBT_EXECUTABLE="$PWD/dbt/.venv/bin/dbt"`. Without `TEST_DATABASE_URL` the PostgreSQL
tests are skipped; to run only the offline tests, use
`uv run --locked pytest -m 'not integration' -q`.

Native commands read `DATABASE_URL` and, optionally, `DATABASE_PASSWORD_FILE`; the app
does not load `.env` by itself:

```powershell
$env:DATABASE_URL='postgresql://transit@127.0.0.1:5432/transit'
$env:DATABASE_PASSWORD_FILE='secrets/postgres_password'
uv run --locked transit --help
uv run --locked uvicorn transit.web.app:app --host 127.0.0.1 --port 8000
```

## Repository layout

| Path | Contents |
| --- | --- |
| `src/transit/ttc/` | TTC realtime and schedule collection, vehicle matching, stop passages, retention |
| `src/transit/bikeshare/` | Bike Share live collection, ridership summaries, dashboard queries |
| `src/transit/web/` | FastAPI app and the site (plain HTML, CSS and JavaScript, no build step) |
| `src/transit/` | CLI (`transit`), scheduler, neighbouring agencies, regional trains, roads, events, bike lanes, weather, trip planner inputs, street map, open-data export |
| `src/transit/migrations/` | Numbered schema migrations |
| `dbt/` | Staging and analytics models and tests |
| `airflow/` | DAGs and their tests |
| `dashboard/` | Streamlit Bike Share dashboard |
| `docker/`, `deploy/` | Trip planner runner, production Compose files, backups |
| `tests/` | Unit and PostgreSQL integration tests on recorded real responses |

## Troubleshooting

- **Docker unavailable:** start Docker Desktop and check `docker info`; a Compose CLI
  alone is not an engine.
- **Database unavailable:** `docker compose ps` and `docker compose logs postgres`.
  Changing `secrets/postgres_password` does not change an existing PostgreSQL volume's
  password.
- **No trains on the site:** `docker compose logs --tail 20 ttc-collector`; `/health`
  reports `degraded` when predictions are more than 2 minutes old.
- **No Directions button:** the trip planner is not running or is still building;
  `docker compose --profile planner logs planner`.
- **Port collision:** set `POSTGRES_PORT`, `WEB_PORT`, `DASHBOARD_PORT` or
  `AIRFLOW_PORT` in `.env`.

Source-specific troubleshooting is in the [TTC](ttc.md) and [Bike Share](bikeshare.md)
documents.
