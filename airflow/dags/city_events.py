"""Daily load of the City of Toronto events calendar; skipped when the feed is unchanged."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="city_events",
    description="Load City of Toronto festivals and events for the coming 60 days",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    schedule="50 9 * * *",  # 05:50 Toronto time (EDT).
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["toronto", "events"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "events-refresh"], check=True, timeout=840)

    refresh()
