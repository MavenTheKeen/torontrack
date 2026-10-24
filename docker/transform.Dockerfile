FROM ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 AS uv

FROM mirror.gcr.io/library/python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY dbt ./dbt
RUN UV_PROJECT_ENVIRONMENT=/opt/app-venv uv sync --locked --no-dev --no-editable --no-cache \
    && UV_PROJECT_ENVIRONMENT=/opt/dbt-venv uv sync --project /app/dbt --locked --no-dev --no-cache \
    && useradd --create-home --uid 10001 app

USER app
ENTRYPOINT ["/opt/app-venv/bin/transit"]
CMD ["transform", "--dbt-executable", "/opt/dbt-venv/bin/dbt", "--project-dir", "/app/dbt"]
