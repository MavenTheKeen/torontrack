# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/MavenTheKeen/torontrack/security/advisories/new)
rather than in a public issue. Reports are acknowledged within a week. Only the current
`main` branch and the live site are supported. The site gives the same address at
`/.well-known/security.txt`.

## Design

### Web application

- The site reads, with one exception. In production it connects with a read-only
  database login that cannot see the raw feed tables. The exception is a visitor's
  notification subscription: the login may call three functions in the `push` schema
  (set a subscription's lines, read them, read the public key) and cannot read or write
  the tables behind them.
- A subscription is an address this server later sends requests to, so only the browser
  makers' push services are accepted (Google, Mozilla, Apple, Microsoft), over HTTPS,
  checked when offered and again before each send. The table is capped at 5,000 rows and
  the endpoint shares the trip planner's lower rate limit.
- Notifications are encrypted for the one browser (RFC 8291) and signed with the site's
  key (RFC 8292). The private key is a secret file read only by the alerts process, not
  by the public web process.
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
