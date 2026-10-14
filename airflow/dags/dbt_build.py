"""dbt build on its own, for a machine whose collected data arrives by replication.

In the full stack the Bike Share DAG collects and then builds dbt. When collection runs on
a separate server (docs/collector.md), that DAG is paused here and this one keeps the
analytics current instead.
"""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"
DBT = "/opt/dbt-venv/bin/dbt"

with DAG(
    dag_id="dbt_build",
    description="Build and test the dbt models on replicated collector data",
    start_date=datetime(2026, 10, 14, tzinfo=UTC),
    schedule="5,20,35,50 * * * *",
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "execution_timeout": timedelta(minutes=12),
    },
    tags=["toronto", "dbt"],
) as dag:

    @task
    def transform() -> None:
        subprocess.run(
            [APP, "transform", "--dbt-executable", DBT, "--project-dir", "/app/dbt"],
            check=True,
            timeout=660,
        )

    transform()
