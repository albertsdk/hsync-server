# NOTE: The build context must be the repository root (the parent of
# hsync-server), because hsync-server depends on the local ../hsync-common
# package. Build via `./docker.sh build`, which sets the correct context and
# passes `-f hsync-server/Dockerfile`.

### Builder
FROM python:3.13.11-alpine3.22 AS builder

# Install build dependencies in a single layer
RUN apk add --no-cache \
    poetry \
    gcc \
    python3-dev \
    musl-dev \
    linux-headers \
    libffi-dev \
    git

WORKDIR /project

# Set environment variables for Poetry
ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=1 \
    POETRY_VIRTUALENVS_CREATE=1 \
    POETRY_CACHE_DIR=/tmp/poetry_cache

# Add minimal files needed for poetry install
RUN touch README.md
COPY hsync-server/pyproject.toml hsync-server/poetry.lock /project/

# Copy the local path dependency so Poetry can resolve hsync-common,
# declared as {path = "../hsync-common"} -> /hsync-common at build time
COPY hsync-common /hsync-common

# Install dependencies and clean cache in single layer
RUN poetry env use python3.13 \
    && poetry install --only main --no-root \
    && rm -rf /tmp/poetry_cache

### Run Image
FROM python:3.13.11-alpine3.22

WORKDIR /project/

# Copy the virtual environment from the builder stage
COPY --from=builder /project/.venv /venv

# Copy the editable path dependency to the same absolute path used at build
# time so the venv's editable install of hsync-common resolves at runtime
COPY hsync-common /hsync-common

# Copy application source code
COPY hsync-server/hsync_server /project/hsync_server

# Setup directories and permissions in single layer
RUN addgroup -S hsync \
    && adduser -S -G hsync -h /project hsync \
    && mkdir -p buckets tmp certs \
    && ln -s /venv /project/.venv \
    && chown -R hsync:hsync buckets tmp certs

# Set environment variables with defaults
# These can be overridden via --env-file or -e flags
ENV PATH="/project/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    HSYNC_LOG_LEVEL=info \
    HSYNC_PORT=8765 \
    HSYNC_WORKERS=1 \
    HSYNC_TZ=UTC \
    HSYNC_BUCKETS=/project/buckets \
    HSYNC_SSL_CERT_DIR=/project/certs

# Expose default port (can be changed via HSYNC_PORT)
EXPOSE 8765

# Run as an unprivileged application user for security
USER hsync

# Launch through the application entrypoint (not uvicorn directly): it is the
# single place that reads HSYNC_PORT / HSYNC_LOG_LEVEL / HSYNC_WORKERS and the
# HSYNC_SSL_* settings and wires SSL into uvicorn. Invoking uvicorn directly
# would ignore HSYNC_SSL_ENABLED and always serve plaintext HTTP. The module is
# run with `python -m` because the image installs deps with `--no-root`, so the
# `hsync-server` console script is intentionally not generated.
CMD ["python", "-m", "hsync_server"]
