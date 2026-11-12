"""Daily load of City of Toronto wards and 311 service requests; unchanged years are skipped."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="service_requests",
    description="Load City of Toronto wards and 311 service requests",
    start_date=datetime(2026, 11, 12, tzinfo=UTC),
    schedule="50 10 * * *",  # 05:50 Toronto time (EST).
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=30),
    },
    tags=["toronto", "311"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "neighbourhood-refresh"], check=True, timeout=1740)

    refresh()
