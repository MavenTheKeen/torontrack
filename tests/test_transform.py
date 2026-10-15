import subprocess
import threading
import time

import psycopg
import pytest

from transit import transform
from transit.db import connect
from transit.transform import dbt_environment


def test_dbt_configuration_stays_in_environment_and_decodes_url_password():
    env = dbt_environment("postgresql://tester:p%40ss@db.example:5544/transit?sslmode=require")
    assert env["DBT_USER"] == "tester"
    assert env["DBT_PASSWORD"] == "p@ss"
    assert env["DBT_HOST"] == "db.example"
    assert env["DBT_PORT"] == "5544"
    assert env["DBT_DATABASE"] == "transit"
    assert env["DBT_SSLMODE"] == "require"


@pytest.mark.integration
def test_a_timed_out_build_ends_its_queries_left_on_the_server(settings, tmp_path, monkeypatch):
    (tmp_path / "dbt_project.yml").write_text("name: test\n")
    left_running = []

    def slow_query():
        try:
            with psycopg.connect(settings.database_url, application_name="dbt") as conn:
                conn.execute("SELECT pg_sleep(60)")
            left_running.append("finished")
        except psycopg.OperationalError:
            left_running.append("ended")

    def timed_out_dbt(*args, timeout, **kwargs):
        # dbt is killed on timeout, but the query it had sent keeps running.
        query = threading.Thread(target=slow_query)
        query.start()
        time.sleep(1)
        raise subprocess.TimeoutExpired("dbt", timeout)

    monkeypatch.setattr(transform.subprocess, "run", timed_out_dbt)
    with pytest.raises(subprocess.TimeoutExpired):
        transform.run_transform(settings.database_url, project_dir=tmp_path)
    for _ in range(50):
        if left_running:
            break
        time.sleep(0.1)
    assert left_running == ["ended"]
    with connect(settings.database_url) as conn:
        run = conn.execute("SELECT status, error FROM ops.transformation_runs").fetchone()
    assert run == {"status": "failed", "error": "TimeoutExpired"}
