"""The production job scheduler that replaces Airflow (transit.scheduler)."""

import subprocess
from datetime import UTC, datetime

from transit import scheduler


def at(hour, minute, day=16):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)  # 16 October 2026: Friday.


def test_cron_expressions_match_their_minutes():
    assert scheduler.due("*/15 * * * *", at(9, 45))
    assert not scheduler.due("*/15 * * * *", at(9, 44))
    assert scheduler.due("20 */6 * * *", at(18, 20)) and not scheduler.due(
        "20 */6 * * *", at(19, 20)
    )
    assert scheduler.due("0 9 * * 1-5", at(9, 0)) and not scheduler.due("0 9 * * 0,6", at(9, 0))


def test_every_airflow_dag_has_a_job_with_its_schedule():
    jobs = {j.name: j.cron for j in scheduler.JOBS}
    assert len(jobs) == 12
    assert jobs["planner_data"] == "20 10 * * *" and jobs["road_restrictions"] == "*/10 * * * *"


def test_a_failing_job_is_retried_and_the_reliability_run_keeps_its_identity():
    calls = []

    def runner(argv, check, timeout):
        calls.append(argv[3:])
        if len(calls) == 1:
            raise subprocess.CalledProcessError(1, argv)

    job = scheduler.JOBS[0]
    assert scheduler.run_job(job, at(9, 45), runner=runner, sleep=lambda s: None)
    # Attempt 1 failed at collect; attempt 2 ran collect, replay and transform.
    assert [c[0] for c in calls] == ["collect", "collect", "replay", "transform"]
    assert calls[0][2] == calls[1][2] == calls[2][2]
    assert calls[0][2] != scheduler.steps_for(job, at(10, 0))[0][2]


def test_a_job_that_keeps_failing_gives_up_after_its_retries():
    def runner(argv, check, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    job = scheduler.Job("x", "* * * * *", (("weather-refresh",),))
    assert not scheduler.run_job(job, at(9, 0), runner=runner, sleep=lambda s: None)
