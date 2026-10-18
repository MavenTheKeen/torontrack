# Security design

- The Bike Share collector accepts only HTTPS URLs on `toronto.publicbikesystem.net`,
  using the default HTTPS port or port 443, without URL credentials.
- HTTP redirects are disabled. Decoded feed responses are capped at 10 MiB.
- Database queries use parameterized values; dynamic database names use quoted
  SQL identifiers.
- Docker Compose binds PostgreSQL, Streamlit, Airflow and the site to `127.0.0.1`.
- In production ([deployment](docs/deploy.md)) the site connects with a read-only
  database login, sits behind a reverse proxy that terminates HTTPS, applies per-client
  rate limits and sends a strict Content Security Policy. The site also sends a
  Permissions-Policy that allows only location (used for "near me" and trip guidance),
  cross-origin isolation headers, and HSTS on HTTPS responses, whichever proxy is in front. Airflow and the dashboard are
  reached only through an SSH tunnel.

The base `compose.yaml` alone is for local development: it uses one shared database
account and Airflow's development access settings.
