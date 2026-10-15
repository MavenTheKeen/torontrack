"""Ticketmaster concerts, games and shows for the Events page, every 6 hours."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="ticketed_events",
    description="Load Ticketmaster events for the next 14 days",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    schedule="20 */6 * * *",  # 14 days x 1 request: about 56 requests a day of 5,000 allowed.
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
        subprocess.run([APP, "ticketed-events-refresh"], check=True, timeout=840)

    refresh()
