# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/MavenTheKeen/torontrack/security/advisories/new)
rather than in a public issue. Reports are acknowledged within a week. Only the current
`main` branch and the live site are supported.

## Design

### Web application

- The site only reads. In production it connects with a read-only database login that
  cannot see the raw feed tables.
- Database queries use parameterized values; dynamic database names use quoted SQL
  identifiers. Path and query parameters are checked for length and format before use.
- Every response carries a strict Content Security Policy with no outside origins, a
  Permissions-Policy that allows only location (used for "near me" and trip guidance),
  cross-origin isolation headers, `nosniff` and `no-referrer`. HTTPS responses carry
  HSTS, whichever proxy is in front.
- API requests are rate-limited per client, with a lower limit for trip plans. An IPv6
  client counts as its whole /64, and the number of clients tracked is capped. Behind a
  proxy, only the proxy's address may set the forwarded client address.
- Interactive API documentation is turned off, and no access log of visitors is kept.

### Collectors

- The Bike Share collector accepts only HTTPS URLs on `toronto.publicbikesystem.net`,
  using the default HTTPS port or port 443, without URL credentials.
- HTTP redirects are disabled. Decoded feed responses are capped at 10 MiB.

### Containers and server

- Docker Compose binds PostgreSQL, Streamlit, Airflow and the site to `127.0.0.1` (or,
  behind an existing proxy, to the server's LAN address). Airflow and the dashboard are
  reached only through an SSH tunnel.
- The project's own containers run as an unprivileged user with every Linux capability
  dropped and privilege escalation disabled; the site's container has a read-only
  filesystem.
- Passwords and API keys are files in `./secrets`, mounted as Compose secrets, never
  environment variables ([deployment](docs/deploy.md#secrets)).
- Database dumps are readable only by root.

### Supply chain

- Container images are pinned by digest and Python dependencies by lock file with
  hashes. GitHub Actions are pinned to commits, and the workflow token is read-only.
- CI audits the installed Python packages for known vulnerabilities on every push and
  weekly. Dependabot proposes updates for actions, Python packages and images.

The base `compose.yaml` alone is for local development: it uses one shared database
account and Airflow's development access settings.
