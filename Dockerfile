FROM ghcr.io/astral-sh/uv:0.9.5@sha256:f459f6f73a8c4ef5d69f4e6fbbdb8af751d6fa40ec34b39a1ab469acd6e289b7 AS uv

FROM mirror.gcr.io/library/python:3.15.0-slim-bookworm@sha256:2a41267d9de9d358f77a1de356e0f16ecba8d39827855be0ce9ef4792edce402

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
