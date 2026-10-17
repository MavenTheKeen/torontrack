#!/bin/sh
# Deploy the newest version on the server (docs/deploy.md#deploy-a-new-version).
#
# The order matters. The site's queries can need a table or column that only the new dbt
# models create, so migrations run and the models are rebuilt with the new code before
# the new web service starts. Restarting first left /api/reliability failing for about
# two minutes on 2026-10-16, until the scheduler's next build.
#
# Compose files and profiles come from COMPOSE_FILE and COMPOSE_PROFILES in .env, for
# example COMPOSE_FILE=compose.yaml:compose.prod.yaml:compose.proxied.yaml and
# COMPOSE_PROFILES=scheduler,planner. Without COMPOSE_FILE, the production pair is used.
set -eu
cd "$(dirname "$0")/.."

if ! grep -q '^COMPOSE_FILE=' .env 2>/dev/null; then
  export COMPOSE_FILE=compose.yaml:compose.prod.yaml
fi

git pull --ff-only
docker compose --profile tools --profile scheduler build --quiet
docker compose run --rm init-db

# The scheduler may be in the middle of its own dbt build, which holds the transform
# lock; wait for it rather than fail.
for attempt in 1 2 3 4 5; do
  if docker compose --profile scheduler run --rm --no-deps scheduler transform \
      --dbt-executable /opt/dbt-venv/bin/dbt --project-dir /app/dbt >/dev/null; then
    break
  fi
  [ "$attempt" = 5 ] && { echo "dbt build failed; services were not restarted" >&2; exit 1; }
  echo "dbt build busy or failed; retrying in 60 s"
  sleep 60
done

# Everything is restarted, but only the site is waited for: the trip planner can take
# several minutes to rebuild its graph when its inputs changed.
docker compose up -d
docker compose up -d --wait web
docker compose ps --format '{{.Service}}: {{.Status}}'
