"""Daily open-data export: completed days as Parquet and CSV files for download."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="open_data_export",
    description="Write each completed day's headways and dock availability as open data",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    # 09:30 UTC is after the TTC service day ends at 04:00 Toronto time, in EDT and EST.
    schedule="30 9 * * *",
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=20),
    },
    tags=["toronto", "open-data"],
) as dag:

    @task
    def export() -> None:
        # Writes every completed day not yet exported, so a missed run catches up.
        subprocess.run([APP, "export"], check=True, timeout=1140)

    export()
