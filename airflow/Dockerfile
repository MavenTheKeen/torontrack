FROM ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 AS uv

FROM apache/airflow:3.4.0-python3.12@sha256:818f94affc5bd7ce0be0cc04f2ace09aa8382e65190a73a08881229ff32700cb
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never

USER root
RUN mkdir -p /app /opt/app-venv /opt/dbt-venv /opt/airflow/state /opt/airflow/open-data /opt/airflow/planner \
    && chown airflow:root /app /opt/app-venv /opt/dbt-venv /opt/airflow/state /opt/airflow/open-data /opt/airflow/planner \
    && chmod g+w /app /opt/app-venv /opt/dbt-venv /opt/airflow/state /opt/airflow/open-data /opt/airflow/planner
USER airflow

WORKDIR /app
COPY --chown=airflow:root pyproject.toml uv.lock ./
COPY --chown=airflow:root src ./src
COPY --chown=airflow:root dbt ./dbt
RUN UV_PROJECT_ENVIRONMENT=/opt/app-venv uv sync --python /usr/local/bin/python --locked --no-dev --no-editable --no-cache \
    && UV_PROJECT_ENVIRONMENT=/opt/dbt-venv uv sync --project /app/dbt --python /usr/local/bin/python --locked --no-dev --no-cache

COPY --chown=airflow:root airflow/dags /opt/airflow/dags
COPY --chown=airflow:root airflow/healthcheck.py /opt/airflow/healthcheck.py
WORKDIR /opt/airflow
CMD ["standalone"]
