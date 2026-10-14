"""PostgreSQL connections and schema setup shared by every pipeline."""

from importlib.resources import files

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from transit import locks

# The original schema files are frozen baselines. Every later change is a numbered file
# in transit/migrations, applied once and recorded in ops.schema_migrations.
BASELINES = (
    ("0000_baseline_bikeshare", "transit.bikeshare"),
    ("0000_baseline_ttc", "transit.ttc"),
)
# Everything the public site reads. raw holds source payloads it never needs.
READONLY_SCHEMAS = ("normalized", "staging", "analytics", "ops")


def connect(database_url):
    return psycopg.connect(
        database_url,
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=10,
        options="-c timezone=UTC -c statement_timeout=60000",
    )


def migrations() -> list[tuple[str, str]]:
    """(version, SQL) in the order they apply: baselines, then numbered migrations."""
    steps = [
        (version, files(package).joinpath("schema.sql").read_text(encoding="utf-8"))
        for version, package in BASELINES
    ]
    folder = files("transit").joinpath("migrations")
    for path in sorted((p for p in folder.iterdir() if p.name.endswith(".sql")), key=str):
        steps.append((path.name.removesuffix(".sql"), path.read_text(encoding="utf-8")))
    return steps


def init_db(database_url, upto: str | None = None) -> list[str]:
    """Apply pending schema steps in one transaction; return the versions applied.

    A database created before migrations existed has no ops.schema_migrations; its
    baselines re-run harmlessly (they only create what is missing) and are recorded.
    `upto` stops after that version (used to test a migration against older data).
    """
    applied = []
    with connect(database_url) as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (locks.SCHEMA_MIGRATION,))
        conn.execute("CREATE SCHEMA IF NOT EXISTS ops")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ops.schema_migrations (
                   version text PRIMARY KEY,
                   applied_at timestamptz NOT NULL DEFAULT now())"""
        )
        done = {r["version"] for r in conn.execute("SELECT version FROM ops.schema_migrations")}
        for version, statements in migrations():
            if version not in done:
                conn.execute(statements)
                conn.execute("INSERT INTO ops.schema_migrations (version) VALUES (%s)", (version,))
                applied.append(version)
            if version == upto:
                break
    return applied


def ensure_readonly_role(database_url, role: str, password: str):
    """Create or update a login role that can read the site's schemas and write nothing,
    including tables dbt creates later (default privileges for objects this user makes)."""
    with connect(database_url) as conn, conn.transaction():
        name = sql.Identifier(role)
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        verb = sql.SQL("ALTER" if exists else "CREATE")
        conn.execute(
            sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                verb, name, sql.Literal(password)
            )
        )
        conn.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(name))
        database = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(database), name)
        )
        for schema in READONLY_SCHEMAS:
            s = sql.Identifier(schema)
            conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(s))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(s, name))
            conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(s, name))
            conn.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA {} GRANT SELECT ON TABLES TO {}"
                ).format(s, name)
            )
