"""Daily download of the trip planner's inputs; the planner rebuilds when any of them change."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="planner_data",
    description="Download TTC and regional schedules and OpenStreetMap streets for the planner",
    start_date=datetime(2026, 10, 13, tzinfo=UTC),
    # 06:20 Toronto time (EDT), after BBBike's overnight extract and before the morning peak.
    schedule="20 10 * * *",
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["toronto", "planner"],
) as dag:

    @task
    def download() -> None:
        subprocess.run(
            [APP, "planner-data", "--out", "/opt/airflow/planner"], check=True, timeout=840
        )

    download()
