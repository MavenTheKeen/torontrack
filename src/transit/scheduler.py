"""A small job scheduler for production, in place of Airflow (docs/deploy.md).

The Airflow DAGs in airflow/dags only run `transit` commands on cron schedules, and
Airflow itself takes about 1.1 GB of memory. This runs the same commands on the same
schedules (UTC), each at most once at a time, with the DAGs' retries and timeouts, and
logs every run as one JSON line. Runs missed while it was down are not caught up, as
with the DAGs' catchup=False. Airflow remains available locally (compose profile
`airflow`).
"""

import hashlib
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DBT = "/opt/dbt-venv/bin/dbt"
DBT_PROJECT = "/app/dbt"
PLANNER_DIR = "/opt/airflow/planner"
HEARTBEAT = Path("/tmp/scheduler-heartbeat")
RETRIES = 2
RETRY_DELAY_S = 120


@dataclass(frozen=True)
class Job:
    name: str
    cron: str
    steps: tuple  # Each step is a tuple of `transit` arguments, run in order.
    timeout_min: int = 15


def _collection_steps(identity: str) -> tuple:
    return (
        ("collect", "--collection-id", identity),
        ("replay", "--collection-id", identity),
        ("transform", "--dbt-executable", DBT, "--project-dir", DBT_PROJECT),
    )


JOBS = (
    Job("reliability", "*/15 * * * *", (), timeout_min=12),  # Steps built per run.
    Job("road_restrictions", "*/10 * * * *", (("roads-refresh",),), timeout_min=5),
    Job("weather_hourly", "20 * * * *", (("weather-refresh",),), timeout_min=10),
    Job("ticketed_events", "20 */6 * * *", (("ticketed-events-refresh",),)),
    Job("ttc_static_gtfs", "0 9 * * *", (("ttc-gtfs-refresh",),)),
    Job("regional_schedule", "10 9 * * *", (("regional-refresh",),)),
    Job("agency_schedules", "20 9 * * *", (("agencies-refresh",),)),
    Job("open_data_export", "30 9 * * *", (("export",),), timeout_min=20),
    Job("bikeshare_trips", "40 9 * * *", (("bikeshare-trips-refresh",),), timeout_min=45),
    Job("city_events", "50 9 * * *", (("events-refresh",),)),
    Job("ttc_official_delays", "0 10 * * *", (("ttc-delays-refresh",),)),
    Job("planner_data", "20 10 * * *", (("planner-data", "--out", PLANNER_DIR),)),
)


def _field_matches(field: str, value: int) -> bool:
    for part in field.split(","):
        if part == "*":
            return True
        if part.startswith("*/"):
            if value % int(part[2:]) == 0:
                return True
        elif "-" in part:
            low, high = (int(x) for x in part.split("-"))
            if low <= value <= high:
                return True
        elif int(part) == value:
            return True
    return False


def due(cron: str, at: datetime) -> bool:
    """Whether a five-field cron expression (minute hour day month weekday, Sunday 0)
    matches the minute `at`."""
    minute, hour, day, month, weekday = cron.split()
    return (
        _field_matches(minute, at.minute)
        and _field_matches(hour, at.hour)
        and _field_matches(day, at.day)
        and _field_matches(month, at.month)
        and _field_matches(weekday, (at.weekday() + 1) % 7)
    )


def steps_for(job: Job, slot: datetime) -> tuple:
    if job.name == "reliability":
        # Stable across retries of one run, distinct across runs (as the DAG's run_id).
        digest = hashlib.sha256(f"reliability:{slot.isoformat()}".encode()).hexdigest()
        return _collection_steps(f"scheduler-{digest}")
    return job.steps


def run_job(job: Job, slot: datetime, runner=subprocess.run, sleep=time.sleep) -> bool:
    """Run a job's steps in order, retrying the whole job up to RETRIES times."""
    steps = steps_for(job, slot)
    for attempt in range(1, RETRIES + 2):
        started = time.monotonic()
        try:
            for step in steps:
                runner(
                    [sys.executable, "-m", "transit.cli", *step],
                    check=True,
                    timeout=job.timeout_min * 60,
                )
            _log({"job": job.name, "status": "ok", "attempt": attempt,
                  "seconds": round(time.monotonic() - started)})  # fmt: skip
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            _log({"job": job.name, "status": "failed", "attempt": attempt,
                  "error": type(exc).__name__})  # fmt: skip
            if attempt <= RETRIES:
                sleep(RETRY_DELAY_S * attempt)
    return False


def run_forever(stop: threading.Event | None = None, jobs=JOBS, clock=None) -> None:
    stop = stop or threading.Event()
    clock = clock or (lambda: datetime.now(UTC))
    running: dict[str, threading.Thread] = {}
    last_slot = None
    while not stop.is_set():
        now = clock()
        slot = now.replace(second=0, microsecond=0)
        if slot != last_slot:
            last_slot = slot
            for job in jobs:
                busy = running.get(job.name)
                if due(job.cron, slot) and not (busy and busy.is_alive()):
                    thread = threading.Thread(target=run_job, args=(job, slot), daemon=True)
                    running[job.name] = thread
                    thread.start()
        HEARTBEAT.write_text(now.isoformat())
        stop.wait(max(1.0, 60 - clock().second))


def healthy(max_age_s: int = 180) -> bool:
    try:
        beat = datetime.fromisoformat(HEARTBEAT.read_text())
    except (OSError, ValueError):
        return False
    return (datetime.now(UTC) - beat).total_seconds() < max_age_s


def _log(record: dict) -> None:
    print(json.dumps({"at": datetime.now(UTC).isoformat(), **record}), flush=True)
