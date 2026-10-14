"""Run dbt in its own environment and persist compact, inspectable test outcomes."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from transit import locks
from transit.db import connect

TRANSFORM_LOCK = locks.DBT_TRANSFORM


def dbt_environment(database_url: str) -> dict[str, str]:
    config = conninfo_to_dict(database_url)
    env = os.environ.copy()
    env.update(
        {
            "DBT_HOST": config.get("host", "localhost"),
            "DBT_PORT": config.get("port", "5432"),
            "DBT_USER": config.get("user", "transit"),
            "DBT_PASSWORD": config.get("password", ""),
            "DBT_DATABASE": config.get("dbname", "transit"),
            "DBT_SSLMODE": config.get("sslmode", "prefer"),
            "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
            "PGTZ": "UTC",
        }
    )
    return env


def run_transform(
    database_url: str, executable="dbt", project_dir="dbt", full_refresh=False
) -> dict:
    project = Path(project_dir).resolve()
    if not (project / "dbt_project.yml").is_file():
        raise ValueError(f"dbt project not found: {project}")
    invocation = str(uuid4())
    with connect(database_url) as conn:
        if not conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (TRANSFORM_LOCK,)
        ).fetchone()["locked"]:
            raise RuntimeError("Another dbt build is running")
        try:
            conn.execute(
                """UPDATE ops.transformation_runs SET status='failed',finished_at=now(),
                   error='Interrupted process; recovered by next dbt lock holder'
                   WHERE status='running'"""
            )
            conn.execute(
                """INSERT INTO ops.transformation_runs (transformation_id,status)
                   VALUES (%s,'running')""",
                (invocation,),
            )
            try:
                # Isolated artifacts prevent accidentally importing a previous build's results.
                with tempfile.TemporaryDirectory(prefix="transit-dbt-") as artifacts:
                    result = subprocess.run(
                        [
                            str(executable),
                            "build",
                            "--project-dir",
                            str(project),
                            "--profiles-dir",
                            str(project),
                            "--target-path",
                            artifacts,
                            "--log-path",
                            str(Path(artifacts) / "logs"),
                            *(["--full-refresh"] if full_refresh else []),
                        ],
                        env=dbt_environment(database_url),
                        check=False,
                        timeout=600,
                    )
                    result_file = Path(artifacts) / "run_results.json"
                    raw_results = (
                        json.loads(result_file.read_text()) if result_file.exists() else {}
                    )
                    compact = [
                        {
                            "unique_id": r["unique_id"],
                            "status": r["status"],
                            "failures": r.get("failures"),
                            "execution_time": r.get("execution_time"),
                        }
                        for r in raw_results.get("results", [])
                    ]
                successful = result.returncode == 0 and bool(compact)
                conn.execute(
                    """UPDATE ops.transformation_runs SET status=%s, finished_at=now(),
                       dbt_results=%s,error=%s WHERE transformation_id=%s""",
                    (
                        "succeeded" if successful else "failed",
                        Jsonb(compact),
                        None if successful else f"dbt exit {result.returncode}; inspect task logs",
                        invocation,
                    ),
                )
                if not successful:
                    raise RuntimeError("dbt build failed; recorded in ops.transformation_runs")
            except Exception as exc:
                conn.execute(
                    """UPDATE ops.transformation_runs SET status='failed', finished_at=now(),
                       error=coalesce(error,%s) WHERE transformation_id=%s""",
                    (type(exc).__name__, invocation),
                )
                raise
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (TRANSFORM_LOCK,))
    return {"transformation_id": invocation, "status": "succeeded", "dbt_results": len(compact)}
