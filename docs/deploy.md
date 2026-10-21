# Deployment

The whole stack runs on one Linux server with Docker Compose: the same services as
locally, plus Caddy for HTTPS and a daily database backup. `compose.prod.yaml` holds the
production differences; the base `compose.yaml` is unchanged.

```text
Internet ──443──> Caddy ──> web (FastAPI, read-only DB login)
                                   │
   ttc-collector ──> PostgreSQL <──┘   (127.0.0.1 only)
   scheduler ──────> PostgreSQL        (scheduled jobs, transit.scheduler)
   dashboard ──────> PostgreSQL        (127.0.0.1:8501, SSH tunnel)
   backup ─────────> ./backups         (daily pg_dump, 7 days kept)
```

## What production changes

| Concern | How |
| --- | --- |
| HTTPS | Caddy obtains and renews a certificate for `SITE_ADDRESS` automatically. A free `<ip-with-dashes>.sslip.io` name works until a domain is bought. |
| Exposure | Only Caddy publishes ports (80, 443). PostgreSQL and the dashboard keep the base file's `127.0.0.1` bindings. |
| Secrets | Passwords, the Ticketmaster key and the alert URLs are files in `./secrets`, mounted into only the containers that use them ([Secrets](#secrets)). They are not in any container's environment, so `docker inspect` and child processes do not see them. |
| Least privilege | `init-db` creates a `transit_web` login with the password in `secrets/web_db_password`: `SELECT` on `normalized`, `staging`, `analytics` and `ops` (including tables dbt creates later), no access to `raw`, read-only transactions. The site connects with it. |
| Rate limiting | Behind a proxy every request comes from Caddy. Caddy sets `X-Forwarded-For` to the connecting client (it ignores the header from untrusted clients), and uvicorn runs with `--proxy-headers`, so the per-client limit applies to real visitors. |
| Privacy | Neither Caddy nor the site keeps an access log, so visitor addresses are not recorded. |
| Backups | The `backup` service writes a zstd-compressed `pg_dump` custom-format file to `./backups` daily and keeps `BACKUP_KEEP_DAYS` (7). Raw TTC snapshots (a 3-day replay buffer) and their issue rows are left out, so a dump is 21 MB where a default one is 70 MB (2026-10-13); a restore loses only the ability to replay those 3 days. These sit on the same disk; copy them elsewhere for disaster recovery. |
| Logs | Docker's json-file logs rotate at 10 MB × 3 per container (set by `deploy/server-setup.sh`). |

## First deployment

On a fresh Ubuntu 24.04 server (tested on an Oracle Cloud Ampere A1 instance, arm64)
with ports 80 and 443 open in the cloud firewall:

```bash
git clone https://github.com/MavenTheKeen/torontrack.git
cd torontrack
bash deploy/server-setup.sh        # Docker, log rotation, OS firewall, auto updates
exit                               # log in again so the docker group applies
```

Create the secret files (the two database passwords are generated) and `.env` with the
public hostname:

```bash
cd torontrack
sh deploy/make_secrets.sh
# Phone alerts: subscribe to this topic in the ntfy app (docs/alerts.md).
printf 'https://ntfy.sh/transit-alerts-%s' "$(openssl rand -hex 12)" > secrets/ntfy_url
cp .env.example .env
ip=$(curl -s https://checkip.amazonaws.com)
echo "SITE_ADDRESS=${ip//./-}.sslip.io" >> .env
```

Build and start everything, then turn on the schedules:

```bash
alias dc='docker compose -f compose.yaml -f compose.prod.yaml'
dc --profile tools --profile scheduler build
dc --profile scheduler up -d --wait
# The scheduler runs the same jobs as the Airflow DAGs on their schedules (UTC), in
# one small process instead of Airflow's ~1.1 GB; Airflow stays available locally.
dc exec scheduler /opt/app-venv/bin/transit bikeshare-trips-refresh   # first load, a few minutes
dc exec scheduler /opt/app-venv/bin/transit regional-refresh   # first load, then daily
dc exec scheduler /opt/app-venv/bin/transit weather-refresh --since 2026-10-09   # one-time backfill
dc --profile tools run --rm map-tiles   # street map for the site (docs/map.md)
```

The trip planner is optional. It needs about 5 GB of free memory while it rebuilds its
graph and 2.2 GB while serving ([trip planner](planner.md)):

```bash
dc --profile scheduler --profile planner up -d
```

The site is then at `https://<SITE_ADDRESS>`. The first certificate takes a few seconds.

To carry over history from another installation, restore a dump into the new database
before starting the collectors:

```bash
dc up -d --wait postgres
dc exec -T postgres pg_restore -U transit -d transit --no-owner --role=transit < transit.dump
dc exec -T postgres psql -U transit -d transit -q < deploy/fix_sequences.sql
dc --profile scheduler up -d --wait
```

The second step matters when the dump comes from a database that received its
collected tables by logical replication. Replication copies rows but not sequence
positions, so without it the first new rows reuse existing
IDs and the collectors fail with unique violations. It is harmless otherwise.

## Secrets

Each secret is a file in `./secrets`, which Git and Docker builds ignore. Compose mounts
a file at `/run/secrets/<name>` in the containers that declare it, and the application
reads it through a `NAME_FILE` variable holding that path (`transit.config`):

| File | Used by | Contents |
| --- | --- | --- |
| `postgres_password` | PostgreSQL, the collectors, Airflow, the dashboard, backups | Owner login `transit` |
| `web_db_password` | `init-db`, the site (production) | Read-only login `transit_web`; the site has no other |
| `ticketmaster_api_key` | Airflow | Optional ([events](events.md)) |
| `metrolinx_api_key` | `agencies-collector` | Optional; live regional trains ([regional trains](regional.md#live-trains)) |
| `ntfy_url`, `heartbeat_url` | `alerts` | Optional ([alerts](alerts.md)) |

`deploy/make_secrets.sh` creates the missing files: a value still in `.env` (an
install from before 2026-10-15) is moved into its file, missing passwords are
generated, and optional ones are left empty. It never overwrites a file. After it runs,
delete those lines from `.env`. The directory is readable by its owner only; the files
inside are world-readable because the containers' users (postgres, app, airflow) are not
the owner.

To set an optional secret later, write the file and recreate what uses it, for example
`printf '%s' "$KEY" > secrets/ticketmaster_api_key` and `dc --profile scheduler up -d scheduler`.
PostgreSQL applies `postgres_password` only when it creates a new data directory;
changing the file afterwards requires `ALTER ROLE` as well.

## Behind an existing proxy

Where the server's network already has a reverse proxy holding ports 80 and 443 (for
example one Caddy instance serving several sites), that proxy terminates HTTPS and this
stack's Caddy is not started. `compose.proxied.yaml`, used as a third file, publishes the
site on the server's LAN address only and trusts forwarded client addresses from the
proxy alone:

```bash
alias dc='docker compose -f compose.yaml -f compose.prod.yaml -f compose.proxied.yaml'
cat >> .env <<EOF
SITE_ADDRESS=torontrack.example.org   # the public name; used by the proxy, not this stack
WEB_BIND=192.0.2.10                   # this server's LAN address
PROXY_IP=192.0.2.20                   # the proxy's LAN address
EOF
```

The rest of the first deployment is unchanged. On the proxy, a site block forwards to
the server; no `log` directive, so visitor addresses are not recorded:

```caddy
torontrack.example.org {
	encode zstd gzip
	header Strict-Transport-Security "max-age=31536000"
	reverse_proxy 192.0.2.10:8000
}
```

The server's LAN address should be reserved in the network's DHCP server, since the
proxy and `WEB_BIND` both refer to it.

## Operating

```bash
dc ps                                   # health of every service
dc logs --tail 50 ttc-collector         # or web, caddy, scheduler, backup
curl -s https://<SITE_ADDRESS>/health   # ok / degraded / no_data
```

Airflow and the dashboard are reachable only through an SSH tunnel from your machine:

```bash
ssh -L 8080:127.0.0.1:8080 -L 8501:127.0.0.1:8501 ubuntu@<server-ip>
# then open http://localhost:8080 (Airflow) and http://localhost:8501 (dashboard)
```

Deploy a new version:

```bash
sh deploy/update.sh
```

It pulls, builds, applies migrations and rebuilds the dbt models before restarting
anything, because the new site can depend on a table or column that only the new models
create. It reads the Compose files and profiles from `COMPOSE_FILE` and
`COMPOSE_PROFILES` in `.env` (for example `COMPOSE_FILE=compose.yaml:compose.prod.yaml`
and `COMPOSE_PROFILES=scheduler,planner`).

Restore a backup. The dumps are readable only by root, so they are read through the
backup container rather than from the host account:

```bash
dc exec -T backup cat /backups/transit-<stamp>.dump |
  dc exec -T postgres pg_restore -U transit -d transit --clean --if-exists
```

## Not covered yet

- Off-server backup copies (for example to object storage).
- Alerting beyond container health checks; an external uptime check on `/health` is
  the simplest addition.
