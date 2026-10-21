#!/bin/sh
# Create the Docker Compose secret files in ./secrets (docs/deploy.md#secrets).
# Run from the repository root. A value still in .env is moved into its file, so an
# existing install keeps its passwords; afterwards remove those lines from .env. Missing
# database passwords are generated; the optional secrets are left empty. Existing files
# are never overwritten.
set -eu
umask 077
mkdir -p secrets
chmod 700 secrets

from_env() {
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | tail -n 1 | tr -d '\r'
}

random() {
  od -An -N24 -tx1 /dev/urandom | tr -d ' \n'
}

for pair in postgres_password:POSTGRES_PASSWORD web_db_password:WEB_DB_PASSWORD \
  ticketmaster_api_key:TICKETMASTER_API_KEY metrolinx_api_key:METROLINX_API_KEY \
  ntfy_url:NTFY_URL heartbeat_url:HEARTBEAT_URL; do
  file="secrets/${pair%%:*}"
  name="${pair#*:}"
  if [ -e "$file" ]; then
    echo "kept      $file"
    continue
  fi
  value=$(from_env "$name")
  case "$name" in
    *_PASSWORD) [ -n "$value" ] || value=$(random) ;;
  esac
  printf '%s' "$value" > "$file"
  # The directory keeps other users out; the files must be readable by the containers'
  # own users (postgres, app, airflow), which differ from the owner of these files.
  chmod 644 "$file"
  if [ -n "$value" ]; then echo "wrote     $file"; else echo "empty     $file"; fi
done
echo "Now remove POSTGRES_PASSWORD, WEB_DB_PASSWORD, TICKETMASTER_API_KEY,"
echo "METROLINX_API_KEY, NTFY_URL and HEARTBEAT_URL from .env, if present."
