# Deployment

The whole stack runs on one Linux server with Docker Compose: the same services as
locally, plus Caddy for HTTPS and a daily database backup. `compose.prod.yaml` holds the
production differences; the base `compose.yaml` is unchanged.

```text
Internet ──443──> Caddy ──> web (FastAPI, read-only DB login)
                                   │
   ttc-collector ──> PostgreSQL <──┘   (127.0.0.1 only)
   Airflow ────────> PostgreSQL        (127.0.0.1:8080, SSH tunnel)
   dashboard ──────> PostgreSQL        (127.0.0.1:8501, SSH tunnel)
   backup ─────────> ./backups         (daily pg_dump, 7 days kept)
```

## What production changes

| Concern | How |
| --- | --- |
| HTTPS | Caddy obtains and renews a certificate for `SITE_ADDRESS` automatically. A free `<ip-with-dashes>.sslip.io` name works until a domain is bought. |
| Exposure | Only Caddy publishes ports (80, 443). PostgreSQL, Airflow and the dashboard keep the base file's `127.0.0.1` bindings. |
| Least privilege | `init-db` creates a `transit_web` login when `WEB_DB_PASSWORD` is set: `SELECT` on `normalized`, `staging`, `analytics` and `ops` (including tables dbt creates later), no access to `raw`, read-only transactions. The site connects with it. |
| Rate limiting | Behind a proxy every request comes from Caddy. Caddy sets `X-Forwarded-For` to the connecting client (it ignores the header from untrusted clients), and uvicorn runs with `--proxy-headers`, so the per-client limit applies to real visitors. |
| Privacy | Caddy has no access log, so visitor addresses are not recorded. |
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

Create `.env` from `.env.example` with generated secrets and the public hostname:

```bash
cd torontrack
cp .env.example .env
sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/" .env
ip=$(curl -s https://checkip.amazonaws.com)
cat >> .env <<EOF
SITE_ADDRESS=${ip//./-}.sslip.io
WEB_DB_PASSWORD=$(openssl rand -hex 24)
# Phone alerts: subscribe to this topic in the ntfy app (docs/alerts.md).
NTFY_URL=https://ntfy.sh/transit-alerts-$(openssl rand -hex 12)
EOF
chmod 600 .env
```

Build and start everything, then turn on the schedules:

```bash
alias dc='docker compose -f compose.yaml -f compose.prod.yaml'
dc --profile tools --profile airflow build
dc --profile airflow up -d --wait
dc exec airflow airflow dags unpause toronto_bikeshare_reliability
dc exec airflow airflow dags unpause ttc_static_gtfs
dc exec airflow airflow dags unpause open_data_export
dc exec airflow airflow dags unpause ttc_official_delays
dc exec airflow airflow dags unpause weather_hourly
dc exec airflow airflow dags unpause regional_schedule
dc exec airflow airflow dags unpause road_restrictions
dc exec airflow airflow dags unpause bikeshare_trips
dc exec airflow airflow dags unpause city_events
dc exec airflow /opt/app-venv/bin/transit bikeshare-trips-refresh   # first load, a few minutes
dc exec airflow /opt/app-venv/bin/transit regional-refresh   # first load, then daily
dc exec airflow /opt/app-venv/bin/transit weather-refresh --since 2026-10-09   # one-time backfill
dc --profile tools run --rm map-tiles   # street map for the site (docs/map.md)
```

The trip planner is optional. It needs about 4 GB of free memory while it rebuilds its
graph and 1.5 GB while serving ([trip planner](planner.md)):

```bash
dc --profile airflow --profile planner up -d
dc exec airflow airflow dags unpause planner_data
```

The site is then at `https://<SITE_ADDRESS>`. The first certificate takes a few seconds.

To carry over history from another installation, restore a dump into the new database
before starting the collectors:

```bash
dc up -d --wait postgres
dc exec -T postgres pg_restore -U transit -d transit --no-owner --role=transit < transit.dump
dc --profile airflow up -d --wait
```

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
dc logs --tail 50 ttc-collector         # or web, caddy, airflow, backup
curl -s https://<SITE_ADDRESS>/health   # ok / degraded / no_data
```

Airflow and the dashboard are reachable only through an SSH tunnel from your machine:

```bash
ssh -L 8080:127.0.0.1:8080 -L 8501:127.0.0.1:8501 ubuntu@<server-ip>
# then open http://localhost:8080 (Airflow) and http://localhost:8501 (dashboard)
```

Deploy a new version:

```bash
git pull
dc --profile tools --profile airflow build
dc --profile airflow up -d --wait
```

Restore a backup:

```bash
dc exec -T postgres pg_restore -U transit -d transit --clean --if-exists < backups/transit-<stamp>.dump
```

## Not covered yet

- Off-server backup copies (for example to object storage).
- Alerting beyond container health checks; an external uptime check on `/health` is
  the simplest addition.
