"""Daily check of Bike Share ridership files; a year is reloaded only when its file changed."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="bikeshare_trips",
    description="Load Bike Share ridership summaries for years whose file changed",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    schedule="40 9 * * *",  # 05:40 Toronto time (EDT); the city updates monthly.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=45),
    },
    tags=["toronto", "bikeshare"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "bikeshare-trips-refresh"], check=True, timeout=2640)

    refresh()
