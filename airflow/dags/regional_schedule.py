"""Daily regional train schedule refresh. Loads a new version only when the zip changed."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="regional_schedule",
    description="Download Metrolinx static GTFS and load the train subset as a new feed version",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    schedule="10 9 * * *",  # 05:10 Toronto time (EDT), after the TTC refresh.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["toronto", "regional", "gtfs"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "regional-refresh"], check=True, timeout=840)

    refresh()
