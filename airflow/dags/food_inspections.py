"""Daily load of City of Toronto food inspections (DineSafe) and food business licences,
each skipped when unchanged, then notable places to eat from Wikidata."""

import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, task

APP = "/opt/app-venv/bin/transit"

with DAG(
    dag_id="food_inspections",
    description="Load City of Toronto food premises inspections (DineSafe)",
    start_date=datetime(2026, 11, 8, tzinfo=UTC),
    schedule="10 10 * * *",  # 05:10 Toronto time (EST), after the city's morning refresh.
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(minutes=15),
    },
    tags=["toronto", "food"],
) as dag:

    @task
    def refresh() -> None:
        subprocess.run([APP, "food-inspections-refresh"], check=True, timeout=840)

    @task
    def licences() -> None:
        subprocess.run([APP, "food-licences-refresh"], check=True, timeout=840)

    @task
    def notable() -> None:
        subprocess.run([APP, "food-notable-refresh"], check=True, timeout=300)

    refresh() >> licences() >> notable()
