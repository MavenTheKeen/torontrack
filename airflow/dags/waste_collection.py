"""Daily load of City of Toronto curbside collection areas and pickup schedule."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="waste_collection",
    description="Load City of Toronto curbside collection areas and pickup schedule",
    start_date=datetime(2026, 11, 12, tzinfo=UTC),
    schedule="40 10 * * *",  # 05:40 Toronto time (EST), after the food loads.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=10),
    },
    tags=["toronto", "waste"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "waste-refresh"], check=True, timeout=540)

    refresh()
