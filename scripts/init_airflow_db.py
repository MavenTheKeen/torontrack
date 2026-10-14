"""Create Airflow's separate local metadata database without touching collected data."""

import os
import sys

import psycopg
from psycopg import sql

from transit import locks

DATABASE_NAME = "airflow_metadata"
BOOTSTRAP_LOCK = locks.AIRFLOW_BOOTSTRAP


def main():
    try:
        # CREATE DATABASE cannot run inside a transaction. A session lock makes
        # the existence check and creation safe across overlapping bootstrap jobs.
        with psycopg.connect(
            os.environ["DATABASE_URL"], autocommit=True, connect_timeout=10
        ) as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (BOOTSTRAP_LOCK,))
            try:
                exists = conn.execute(
                    "SELECT 1 FROM pg_database WHERE datname=%s", (DATABASE_NAME,)
                ).fetchone()
                if exists is None:
                    conn.execute(
                        sql.SQL("CREATE DATABASE {} ENCODING 'UTF8'").format(
                            sql.Identifier(DATABASE_NAME)
                        )
                    )
            finally:
                conn.execute("SELECT pg_advisory_unlock(%s)", (BOOTSTRAP_LOCK,))
    except Exception as exc:
        # Connection exceptions can contain a DSN. Keep credentials out of logs.
        print(f"Airflow database setup failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("Airflow metadata database ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
