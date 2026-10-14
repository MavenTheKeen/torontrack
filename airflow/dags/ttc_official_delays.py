"""Daily check for a new month of TTC's official subway delay log (published monthly)."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="ttc_official_delays",
    description="Load TTC's official subway delay log when the city publishes a new month",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    schedule="0 10 * * *",  # Daily; the download is only reloaded when it changed.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=15),
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["toronto", "ttc", "validation"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "ttc-delays-refresh"], check=True, timeout=840)

    refresh()
