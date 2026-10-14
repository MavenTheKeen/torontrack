FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv

FROM public.ecr.aws/docker/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
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
