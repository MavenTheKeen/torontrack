"""City of Toronto road restrictions (construction and closures), every 10 minutes."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="road_restrictions",
    description="Load road restrictions; unchanged responses are not stored again",
    start_date=datetime(2026, 10, 13, tzinfo=UTC),
    schedule="*/10 * * * *",
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=2),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["toronto", "roads"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "roads-refresh"], check=True, timeout=240)

    refresh()
