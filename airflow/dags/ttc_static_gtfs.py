"""Daily TTC static GTFS refresh. Loads a new feed version only when the zip changed."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="ttc_static_gtfs",
    description="Download TTC static GTFS and load the subway subset as a new feed version",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    schedule="0 9 * * *",  # 05:00 Toronto time (EDT), before the morning peak.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["toronto", "ttc", "gtfs"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "ttc-gtfs-refresh"], check=True, timeout=840)

    refresh()
