"""Schedule real observations; a run's logical date never becomes observation time."""

import hashlib
import subprocess
from datetime import UTC, datetime, timedelta

from airflow.sdk import DAG, get_current_context, task

APP = "/opt/app-venv/bin/transit"
DBT = "/opt/dbt-venv/bin/dbt"
DAG_ID = "toronto_bikeshare_reliability"


def collection_id(run_id: str) -> str:
    """Stable, bounded identity across retries/clears, distinct across DAG runs."""
    digest = hashlib.sha256(f"{DAG_ID}:{run_id}".encode()).hexdigest()
    return f"airflow-{digest}"


def invoke(*arguments: str) -> None:
    """Arguments bypass a shell; inherited environment supplies database credentials."""
    subprocess.run([APP, *arguments], check=True, timeout=660)


with DAG(
    dag_id=DAG_ID,
    description="Collect live Toronto GBFS, replay persisted JSON, then build/test dbt views",
    start_date=datetime(2026, 10, 9, tzinfo=UTC),
    schedule="*/15 * * * *",
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=True,
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
        "retry_exponential_backoff": 2.0,
        "max_retry_delay": timedelta(minutes=5),
        "execution_timeout": timedelta(minutes=12),
    },
    tags=["toronto", "gbfs", "portfolio"],
) as dag:

    @task
    def collect() -> str:
        identity = collection_id(get_current_context()["run_id"])
        invoke("collect", "--collection-id", identity)
        return identity

    @task
    def replay(identity: str) -> str:
        # A separate rerunnable boundary proves stored raw JSON is sufficient.
        invoke("replay", "--collection-id", identity)
        return identity

    @task
    def transform(identity: str) -> None:
        # The argument establishes dependency through XCom, not a data transfer.
        print(f"Building analytical views after collection {identity}")
        invoke("transform", "--dbt-executable", DBT, "--project-dir", "/app/dbt")

    transform(replay(collect()))
