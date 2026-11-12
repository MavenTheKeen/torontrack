"""ECCC city page weather and radar images for the weather map, every six minutes."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="weather_now",
    description="Load ECCC city page weather and radar images for Toronto",
    start_date=datetime(2026, 11, 12, tzinfo=UTC),
    schedule="*/6 * * * *",  # Radar images are published every six minutes.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 0,
        "retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(minutes=5),
    },
    tags=["toronto", "weather"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "weather-now-refresh"], check=True, timeout=240)

    refresh()
