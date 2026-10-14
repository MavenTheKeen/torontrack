"""Hourly load of Environment Canada weather observations for downtown Toronto."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="weather_hourly",
    description="Load ECCC hourly weather; each run re-reads two days, so misses catch up",
    start_date=datetime(2026, 10, 13, tzinfo=UTC),
    schedule="20 * * * *",
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(minutes=10),
    },
    tags=["toronto", "weather"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "weather-refresh"], check=True, timeout=540)

    refresh()
