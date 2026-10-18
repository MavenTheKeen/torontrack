FROM ghcr.io/astral-sh/uv:0.9.30@sha256:538e0b39736e7feae937a65983e49d2ab75e1559d35041f9878b7b7e51de91e4 AS uv

FROM mirror.gcr.io/library/python:3.12.15-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

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
