# Engineering decisions

- **One system, GBFS 3.0.** Discover station endpoints from the live discovery
  document. Reject unsupported versions explicitly. No second source or migration
  heuristics. Unknown JSON fields survive in raw storage.
- **PostgreSQL at every data layer.** JSONB source records in `raw`, Python-validated
  station snapshots and observations in `normalized`, dbt views in `staging` and
  `analytics`, and operational records in `ops`. Each distinct station
  name/location/capacity is stored once as a version, and every collection points to the
  versions in effect then, so later changes do not rewrite history.
- **Logical collection identity.** A collection is identified by `collection_id`
  (stored once, with a small integer `collection_key` that observations use);
  `(collection_key, station_id)` is the observation primary key and
  `(collection_id, feed_name)` is unique for raw feeds. Use an existing
  collection ID only to retry that collection. New command invocations default to
  UUIDs. Payload hashes identify content, not observations: identical availability
  collected later remains valid. Feed and station timestamps are not unique keys.
- **Real time, not scheduler time.** `collected_at` is actual status-response storage
  time, `fetched_at` records each feed's acquisition, `source_published_at` is GBFS
  `last_updated`, and `station_reported_at` is `last_reported`. A delayed retry never
  labels today's reading with yesterday's Airflow logical date. All DB connections
  use UTC; display uses America/Toronto including daylight saving time.
- **Raw first, atomic normalized writes.** Each raw feed commits independently.
  Failure to fetch status preserves discovery and metadata. Retrying fetches only
  missing feeds. Normalization replaces the collection's metadata/observations and
  check results in one transaction. A failed replay preserves previously successful
  data and its success status; the failed replay remains visible as a failed attempt.
- **Concurrency.** One PostgreSQL session advisory lock spans network requests and
  writes for this source. The database uniqueness constraints are a second defense.
  A competing collection fails explicitly. A dead process releases its lock; the
  next collector marks interrupted attempts failed. An interrupted running attempt
  remains visible until that recovery happens.
- **Retries.** HTTP has 5-second connect/20-second other timeouts, at most three
  attempts, exponential delays, and Retry-After handling. A delay over 60 seconds
  ends the attempt rather than retrying early. Schema errors and permanent HTTP
  failures are not retried internally. The next allowed request time survives in
  PostgreSQL, so a fresh process or orchestration retry cannot bypass Retry-After.
- **Quality is separate from collection success.** Malformed required fields,
  empty station feeds, duplicate station IDs, and unknown metadata IDs fail the
  collection. Missing individual status rows, stale station reports and stale
  publication times produce visible failed checks; they never become fake zeros.
  Freshness uses a documented 30-minute threshold and 5-minute future tolerance.
- **Store repeated content once (measured, lossless).** After the first day,
  Bike Share storage was growing about 118 MB/day: 97% of station metadata rows were
  repeats, and every raw station list was a new copy that differed only in its
  timestamp. Payload bodies are now content-addressed, station metadata is versioned,
  and per-collection values are stored per collection. The same data went from 36.4 MB
  to 8.0 MB, verified by fingerprinting every row and raw payload before and after.
  `normalized.station_observations` gives readers the original one-row-per-station
  shape. See [Bike Share storage](bikeshare.md#storage-layout).
- **Numbered schema migrations.** The original schema files are frozen baselines;
  every change is a file in `src/transit/migrations`, applied once, in order and in a
  transaction by `init-db`, and recorded in `ops.schema_migrations`. A migration that
  changes data checks its own result before dropping anything, and a test runs each
  one against a database built at the previous schema.
- **A modest project.** No inferred trips, forecasting, paid services, or availability
  guarantee. Analytical views favor inspectable SQL and immediate consistency over
  premature materialization.
- **Official images from Amazon's mirror.** CI runs failed before any test because
  GitHub's runners hit Docker Hub's anonymous pull limit. PostgreSQL, Python and Caddy
  come from `public.ecr.aws/docker/library`, Amazon's mirror of the Docker official images,
  pinned to the same digests as on Docker Hub (verified identical), so every environment
  runs the same bytes. Apache Airflow is not an official image and stays on Docker Hub.
- **Separate dependency environments.** The app, dbt, and Airflow have independent
  pinned dependency sets. The app and dbt use committed uv lockfiles; Airflow uses a
  digest-pinned official image and its official Python constraints. Docker volumes and
  development runtimes are not repository data.

## Deferred production work

A production configuration exists ([deployment](deploy.md): HTTPS, a read-only
database login for the public site, daily backups) but is not running publicly yet.
Still not covered: external alerting, off-server backups, a secrets manager, Bike Share
data retention, and authentication for the dashboard and Airflow (they are only
reachable on localhost or through SSH). These are not claimed for this project.
