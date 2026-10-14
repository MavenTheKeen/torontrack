# Windows setup

Use Docker Desktop with the WSL 2 backend and Linux containers.
Host Python is optional for the Docker workflow.

## Install the system prerequisite

1. Check the current [Docker Desktop Windows requirements](https://docs.docker.com/desktop/setup/install/windows-install/).
   The WSL 2 backend requires WSL 2.1.5 or later, a supported Windows version,
   hardware virtualization enabled in BIOS/UEFI, and at least 8 GB RAM.
2. If WSL is absent, open PowerShell **as administrator**, run `wsl --install`,
   and restart Windows when requested. Use `wsl --update` for an existing WSL
   installation. These are system changes for the user to perform. See
   [Microsoft's WSL installation instructions](https://learn.microsoft.com/en-us/windows/wsl/install).
3. Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
   and choose the WSL 2 backend. Open Docker Desktop after installation, complete
   its first-run setup, and use Linux containers.
4. Open a new ordinary PowerShell window and verify:

```powershell
wsl --version
docker --version
docker compose version
docker info
```

`docker info` must return both client and server details. A client version alone
does not establish that the engine is running. Docker Desktop includes Compose;
use `docker compose` with a space.

## Start the project

Run these commands from the repository root. Copy `.env` only once so that an
existing local password is preserved. Edit it and replace the example password
with a local password containing letters, digits, hyphens, or underscores.

```powershell
Copy-Item .env.example .env
notepad .env
docker compose config --quiet
docker compose build
docker compose up -d postgres
docker compose run --rm init-db
docker compose run --rm collector collect --collection-id first-local-collection
docker compose up -d dashboard
docker compose ps
```

Open <http://localhost:8501>. History starts at the first successful collection.
Wait at least 15 minutes before making a new collection for normal use:

```powershell
docker compose run --rm collector collect
```

Reuse the same collection ID to retry the same logical collection safely. A new
ID means a new observation even when counts have not changed. To normalize an
already stored payload again without requesting the live feed:

```powershell
docker compose run --rm collector replay --collection-id first-local-collection
```

The explicit live parser check does not require a database. Run it only when
checking the real public API:

```powershell
docker compose run --rm --no-deps collector smoke
```

PostgreSQL data persists in a Docker named volume. `docker compose down` stops
the project and keeps data. Adding `--volumes` deletes that collected history;
do not use it during ordinary shutdown. To collect on a 15-minute schedule, follow
the README's "Enable scheduling" section.

## Diagnose startup problems

```powershell
docker compose logs --tail 100 postgres init-db dashboard
docker compose ps -a
```

- **Docker command missing:** reopen PowerShell after installing Docker Desktop.
- **Cannot connect to engine:** open Docker Desktop and wait for its engine;
  confirm Linux containers and run `docker info` again.
- **WSL/virtualization error:** follow the linked Docker and Microsoft setup
  instructions; hardware or Windows feature changes may require a reboot.
- **Host port already allocated:** edit `POSTGRES_PORT` or `DASHBOARD_PORT` in
  `.env`, then repeat `docker compose up -d dashboard`. Container-to-container
  connections keep using port 5432; the dashboard URL uses `DASHBOARD_PORT`.
- **Database authentication fails after changing `.env`:** PostgreSQL applies
  its initial password only when creating a new data directory. Restore the
  previous password or change it inside PostgreSQL; restarting the container
  does not update an existing database password.
- **Dashboard has no observations:** confirm the collection command succeeded.
  Empty dashboards are expected before the first collection.
- **Stale observations:** a running dashboard does not collect data. Run the
  collector and inspect its result. Never substitute fixture data.

The Compose file waits for the database health check and schema initialization
before starting dependent services, using Docker's documented
[startup conditions](https://docs.docker.com/compose/how-tos/startup-order/).
