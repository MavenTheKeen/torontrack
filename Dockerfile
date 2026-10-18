FROM ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 AS uv

FROM mirror.gcr.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH"

# pyosmium (transit osm-layers) links against expat, which the slim image leaves out.
RUN apt-get update && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src
COPY dashboard ./dashboard
COPY scripts ./scripts
RUN uv sync --locked --no-dev --no-editable --no-cache \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /srv/map && chown app /srv/map

USER app
ENTRYPOINT ["transit"]
CMD ["--help"]
