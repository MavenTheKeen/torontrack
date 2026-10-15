"""Daily Brampton Transit, MiWay and York Region Transit schedule refresh
(transit.agencies). Each agency's network is replaced only when its zip changed."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="agency_schedules",
    description="Download Brampton Transit, MiWay and YRT static GTFS and load their bus networks",
    start_date=datetime(2026, 10, 15, tzinfo=UTC),
    schedule="20 9 * * *",  # 05:20 Toronto time (EDT), after the regional refresh.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["gta", "bus", "gtfs"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "agencies-refresh"], check=True, timeout=840)

    refresh()
