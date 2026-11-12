"""Daily load of City of Toronto drop-in skating and swimming, recreation facilities and
beach water quality."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="recreation",
    description="Load City of Toronto drop-in skating and swimming",
    start_date=datetime(2026, 11, 13, tzinfo=UTC),
    schedule="0 11 * * *",  # 06:00 Toronto time (EST).
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=10),
    },
    tags=["toronto", "recreation"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "recreation-refresh"], check=True, timeout=540)

    @task
    def beaches() -> None:
        subprocess.run([APP, "beaches-refresh"], check=True, timeout=540)

    refresh() >> beaches()
